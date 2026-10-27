"""Fetches TROPOMI methane from NASA Earthdata and methane plumes from Carbon Mapper."""
import json
import logging
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

from . import db
from .config import (
    BBOX, CARBONMAPPER_API, CARBONMAPPER_PAGE, NRT_DAYS, NX, NY, QA_MIN, TMP_DIR, BACKFILL_DAYS,
    TROPOMI_NRT, TROPOMI_OFFL,
)
from .geo import cell_index, country_cell_mask, in_country

log = logging.getLogger("ingest")

_logged_in = False


def _now():
    return datetime.now(timezone.utc)


def login():
    """Log in with EARTHDATA_USERNAME/EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) environment variables."""
    global _logged_in
    if _logged_in:
        return True
    import earthaccess

    try:
        auth = earthaccess.login(strategy="environment")
        _logged_in = bool(auth and auth.authenticated)
    except Exception as e:  # earthaccess may raise when credentials are missing
        log.warning("Earthdata login failed: %s", e)
        _logged_in = False
    db.set_meta("auth", "ok" if _logged_in else "missing")
    return _logged_in


def _granule_id(g):
    return g["umm"].get("GranuleUR") or g["meta"]["native-id"]


def _granule_start(g):
    s = g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- TROPOMI -----------------------------------------------------------------

def _grid_file(path):
    """Turns one TROPOMI CH4 L2 file into 0.1° grid sums within the Azerbaijan box."""
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        p = ds.groups["PRODUCT"]
        lat = np.ma.filled(p["latitude"][0], np.nan).astype(float)
        lon = np.ma.filled(p["longitude"][0], np.nan).astype(float)
        box = (lon >= BBOX[0]) & (lon < BBOX[2]) & (lat >= BBOX[1]) & (lat < BBOX[3])
        if not box.any():
            return None
        rows = np.where(box.any(axis=1))[0]
        r0, r1 = rows.min(), rows.max() + 1
        qa = np.ma.filled(p["qa_value"][0, r0:r1, :], 0).astype(float)
        ch4 = np.ma.filled(p["methane_mixing_ratio_bias_corrected"][0, r0:r1, :], np.nan).astype(float)

    lat, lon, box = lat[r0:r1], lon[r0:r1], box[r0:r1]
    ok = box & (qa > QA_MIN) & np.isfinite(ch4) & (ch4 > 1000) & (ch4 < 3000)
    if not ok.any():
        return None
    j, i = cell_index(lon[ok], lat[ok])
    flat = j * NX + i
    s = np.bincount(flat, weights=ch4[ok], minlength=NX * NY)
    n = np.bincount(flat, minlength=NX * NY)
    return s, n


def _store_cells(con, day, source, s, n):
    mask = country_cell_mask().ravel()
    idx = np.nonzero(n)[0]
    con.executemany(
        """INSERT INTO cells(day, source, j, i, inside, s, n) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(day, source, j, i) DO UPDATE SET s = s + excluded.s, n = n + excluded.n""",
        [(day, source, int(k // NX), int(k % NX), int(mask[k]), float(s[k]), int(n[k])) for k in idx],
    )
    return int(n[idx].sum())


def _ingest_product(product, source, start, end):
    import earthaccess

    short_name, version = product
    granules = earthaccess.search_data(
        short_name=short_name, version=version,
        bounding_box=BBOX, temporal=(start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    with db.read() as con:
        done = {r[0] for r in con.execute("SELECT id FROM granules WHERE product = ?", (short_name,))}
    todo = [g for g in granules if _granule_id(g) not in done]
    log.info("%s: %d granules found, %d new", short_name, len(granules), len(todo))

    added = 0
    for g in todo:
        gid = _granule_id(g)
        day = _granule_start(g).date().isoformat()
        tmp = tempfile.mkdtemp(dir=TMP_DIR)
        try:
            files = earthaccess.download([g], local_path=tmp, threads=1)
            nc = [f for f in map(str, files) if f.endswith(".nc")]
            if not nc:
                raise RuntimeError("download returned nothing (check credentials)")
            res = _grid_file(nc[0])
            with db.tx() as con:
                npix = _store_cells(con, day, source, *res) if res else 0
                con.execute(
                    "INSERT OR REPLACE INTO granules(id, product, day, n_pixels, processed_at) VALUES(?,?,?,?,?)",
                    (gid, short_name, day, npix, _now().isoformat()),
                )
            added += 1
            log.info("  %s -> %s, %d pixels", gid, day, npix)
        except Exception as e:
            log.exception("  %s could not be processed", gid)
            db.set_meta("last_error", f"{_now().isoformat()} {gid}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return added


def _prune(oldest):
    """Drops days that fell out of the backfill window so the database stays small."""
    day = oldest.date().isoformat()
    with db.tx() as con:
        removed = con.execute("DELETE FROM cells WHERE day < ?", (day,)).rowcount
        con.execute("DELETE FROM granules WHERE day < ?", (day,))
    if removed:
        log.info("Removed %d grid rows older than %s", removed, day)
        # SQLite does not shrink the file on DELETE; VACUUM must run outside a transaction
        with db.tx() as con:
            con.execute("VACUUM")


def ingest_tropomi():
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not login():
        log.warning("No Earthdata credentials, skipping TROPOMI")
        return
    now = _now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=NRT_DAYS - 1)
    oldest = today - timedelta(days=BACKFILL_DAYS - 1)
    _prune(oldest)
    db.set_meta("tropomi_started", now.isoformat())
    if oldest < cutoff:
        _ingest_product(TROPOMI_OFFL, "OFFL", oldest, cutoff)
    _ingest_product(TROPOMI_NRT, "NRTI", cutoff, now)
    db.set_meta("tropomi_finished", _now().isoformat())


# --- Carbon Mapper ---------------------------------------------------------------

def _fetch_carbonmapper():
    """All published CH4 plumes intersecting the box, page by page."""
    items, offset = [], 0
    while True:
        query = urllib.parse.urlencode(
            [("bbox", v) for v in BBOX]
            + [("plume_gas", "CH4"), ("status", "published"), ("limit", CARBONMAPPER_PAGE), ("offset", offset)]
        )
        req = urllib.request.Request(f"{CARBONMAPPER_API}?{query}", headers={"User-Agent": "azermethane/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = json.load(r)["items"]
        items += page
        if len(page) < CARBONMAPPER_PAGE:
            return items
        offset += CARBONMAPPER_PAGE


def _plume_row(p):
    lon, lat = p["geometry_json"]["coordinates"][:2]
    # Carbon Mapper withholds some estimates (hide_emission); show the plume without a number then
    emission = None if p.get("hide_emission") else p.get("emission_auto")
    return (
        p["plume_id"], p["scene_timestamp"], lat, lon, int(in_country([lon], [lat])[0]),
        emission, p.get("emission_uncertainty_auto") if emission is not None else None,
        p.get("sector"), p.get("platform"), p.get("wind_speed_avg_auto"),
        p.get("plume_png"), json.dumps(p["plume_bounds"]) if p.get("plume_bounds") else None,
    )


def ingest_plumes():
    """Carbon Mapper plume catalogue (Tanager + EMIT). The table is replaced on every run because
    the image links are signed and expire, and Carbon Mapper can withdraw or revise plumes."""
    try:
        rows = [_plume_row(p) for p in _fetch_carbonmapper()]
    except Exception as e:
        log.exception("Could not fetch the Carbon Mapper catalogue")
        db.set_meta("last_error", f"{_now().isoformat()} carbonmapper: {e}")
        return
    with db.tx() as con:
        con.execute("DELETE FROM cm_plumes")
        con.executemany(
            """INSERT OR REPLACE INTO cm_plumes(id, observed, lat, lon, inside, emission, uncertainty,
               sector, platform, wind_speed, png, bounds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    db.set_meta("plumes_finished", _now().isoformat())
    log.info("Carbon Mapper: %d plumes", len(rows))