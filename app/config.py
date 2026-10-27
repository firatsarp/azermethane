import os
from pathlib import Path

# Box covering Azerbaijan (incl. Nakhchivan) and surroundings: (lon_min, lat_min, lon_max, lat_max)
BBOX = (44.0, 37.8, 51.0, 42.3)

# Grid cell size (degrees). 0.1° ≈ 8.5 x 11 km, TROPOMI pixel is 5.5 x 7 km.
GRID = float(os.environ.get("GRID_DEG", "0.1"))
NX = round((BBOX[2] - BBOX[0]) / GRID)
NY = round((BBOX[3] - BBOX[1]) / GRID)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "azermethane.sqlite"
TMP_DIR = DATA_DIR / "tmp"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
COUNTRY_GEOJSON = STATIC_DIR / "azerbaijan.geojson"

# How many days back to fetch (checked on startup and on every run)
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "14"))
# The last NRT_DAYS days come from the NRT (fast) product, older days from OFFL (final)
NRT_DAYS = int(os.environ.get("NRT_DAYS", "5"))
INGEST_INTERVAL_MIN = int(os.environ.get("INGEST_INTERVAL_MIN", "90"))

# Quality threshold recommended by the TROPOMI CH4 product user manual
QA_MIN = 0.5

TROPOMI_OFFL = ("S5P_L2__CH4____HiR", "2")
TROPOMI_NRT = ("S5P_L2__CH4____HiR_NRT", "2")
EMIT_PLUMES = ("EMITL2BCH4PLM", "002")

# Satellites to predict: name, NORAD id, coverage box (sub-point area from which Azerbaijan is visible)
SATELLITES = {
    "s5p": {
        "name": "Sentinel-5P (TROPOMI)",
        "norad": 42969,
        # 2600 km swath: sees the country even when the sub-point is ~8° east/west of it
        "coverage": (36.0, 33.0, 59.0, 47.0),
    },
    "iss": {
        "name": "ISS (NASA EMIT)",
        "norad": 25544,
        # EMIT swath is ~75 km and only does targeted captures: sub-point must be inside the box
        "coverage": BBOX,
    },
}
