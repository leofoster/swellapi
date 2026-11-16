"""Raw buoy readings near any coordinate (NOAA NDBC realtime feeds).

Coverage is NDBC's network: mostly US waters plus some partner buoys. Elsewhere the
lookup returns no stations rather than failing. Every function here is bounded in
time and never raises on upstream trouble.
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import requests

import surfdeck

BASE = "https://www.ndbc.noaa.gov/data/realtime2"
CACHE_TTL = 600            # NDBC updates hourly; 10 min keeps us polite
_cache: dict = {}
_session = requests.Session()

# Standard meteorological file (.txt): NDBC column -> (our name, unit)
MET = {"WDIR": ("wind_dir_deg", "degT"), "WSPD": ("wind_speed_ms", "m/s"), "GST": ("wind_gust_ms", "m/s"),
       "WVHT": ("wave_height_m", "m"), "DPD": ("dominant_period_s", "s"), "APD": ("average_period_s", "s"),
       "MWD": ("wave_dir_deg", "degT"), "PRES": ("pressure_hpa", "hPa"), "ATMP": ("air_temp_c", "degC"),
       "WTMP": ("water_temp_c", "degC"), "DEWP": ("dewpoint_c", "degC"), "VIS": ("visibility_nmi", "nmi"),
       "PTDY": ("pressure_tendency_hpa", "hPa"), "TIDE": ("tide_ft", "ft")}
# Spectral summary file (.spec): the buoy's own swell / wind-wave split
SPEC = {"WVHT": ("wave_height_m", "m"), "SwH": ("swell_height_m", "m"), "SwP": ("swell_period_s", "s"),
        "SwD": ("swell_dir", "compass"), "WWH": ("wind_wave_height_m", "m"), "WWP": ("wind_wave_period_s", "s"),
        "WWD": ("wind_wave_dir", "compass"), "STEEPNESS": ("steepness", ""), "APD": ("average_period_s", "s"),
        "MWD": ("wave_dir_deg", "degT")}
TEXT_COLS = {"SwD", "WWD", "STEEPNESS"}
UNITS = {**{n: u for n, u in MET.values()}, **{n: u for n, u in SPEC.values()}}


def _parse(text: str, cols: dict, hours: float) -> list[dict]:
    """NDBC realtime2 text -> newest-first list of readings within `hours`. 'MM' becomes None."""
    lines = text.strip().splitlines()
    if len(lines) < 3:
        return []
    head = lines[0].lstrip("#").split()
    now = datetime.now(timezone.utc)
    out = []
    for line in lines[2:]:
        v = line.split()
        if len(v) != len(head):
            continue
        try:
            t = datetime(int(v[0]), int(v[1]), int(v[2]), int(v[3]), int(v[4]), tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - t).total_seconds() > hours * 3600:
            break                                          # files are newest first
        row = {"time": t.isoformat().replace("+00:00", "Z")}
        for col, (name, _) in cols.items():
            if col not in head:
                continue
            raw = v[head.index(col)]
            if raw == "MM":
                row[name] = None
            elif col in TEXT_COLS:
                row[name] = raw
            else:
                try:
                    row[name] = float(raw)
                except ValueError:
                    row[name] = None
        out.append(row)
    return out


def _get(station_id: str, kind: str, timeout: float) -> Optional[str]:
    key = (station_id, kind)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        r = _session.get(f"{BASE}/{station_id}.{kind}", timeout=timeout)
        text = r.text if r.status_code == 200 else None
    except Exception:
        text = None
    _cache[key] = (time.monotonic(), text)
    return text


def readings(station_id: str, hours: float = 24, timeout: float = 6) -> dict:
    """Raw readings for one station: standard met (.txt) and spectral swell split (.spec)."""
    with ThreadPoolExecutor(2) as ex:
        met_f, spec_f = ex.submit(_get, station_id, "txt", timeout), ex.submit(_get, station_id, "spec", timeout)
        met_t, spec_t = met_f.result(), spec_f.result()
    met = _parse(met_t, MET, hours) if met_t else []
    spec = _parse(spec_t, SPEC, hours) if spec_t else []
    latest = {}
    for rows in (met, spec):                               # newest non-null value per field
        for r in rows:
            for k, val in r.items():
                if k != "time" and val is not None and k not in latest:
                    latest[k] = val
    times = [r["time"] for r in met[:1] + spec[:1]]
    return {"observed_at": max(times) if times else None, "latest": latest or None,
            "met": met, "spectral": spec, "available": bool(met_t or spec_t)}


def station(station_id: str) -> Optional[dict]:
    surfdeck.nearest_buoys(0, 0, max_km=0)                 # loads / refreshes the station list
    return next((dict(s) for s in surfdeck._stations["list"] if s["id"].lower() == station_id.lower()), None)


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return round((math.degrees(math.atan2(y, x)) + 360) % 360)


def near(lat: float, lon: float, max_km: float = 300, k: int = 5, hours: float = 0) -> list[dict]:
    """Nearest stations with coordinates and their latest raw readings (plus `hours` of history)."""
    found = surfdeck.nearest_buoys(lat, lon, max_km=max_km, k=k)
    with ThreadPoolExecutor(max(1, len(found))) as ex:
        data = list(ex.map(lambda s: readings(s["id"], hours=max(hours, 3)), found))
    out = []
    for s, d in zip(found, data):
        if not hours:
            d = {k: v for k, v in d.items() if k not in ("met", "spectral")}
        out.append({**s, "bearing_deg": bearing_deg(lat, lon, s["lat"], s["lon"]),
                    "url": f"https://www.ndbc.noaa.gov/station_page.php?station={s['id']}", **d})
    return out
