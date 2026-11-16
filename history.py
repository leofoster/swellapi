"""Satellite history for a spot: past Sentinel-2 passes over the break, each with a true-colour
crop and the swell, wind and tide modelled for that hour.

Imagery: ESA Copernicus Sentinel-2 L2A as Cloud-Optimised GeoTIFFs on AWS (Element 84 Earth
Search STAC). Public, no account or key; reads are HTTP range requests of a small window, so a
crop costs a few hundred kB. Passes are around 11:00-12:00 local, every 2-5 days, at 10 m/pixel:
whitewater lines and sandbars show, individual wave faces don't.

Conditions: one Open-Meteo marine + one weather call per spot covering the last 92 days, cached.
"""
import io
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import surfdeck

STAC = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
COG_HOST = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
ITEM_ID = re.compile(r"^S2[A-D]_\d{2}[A-Z]{3}_\d{8}_\d+_L2A$")
MAX_DAYS = 92                    # Open-Meteo past_days limit
CLOUD_CLASSES = (3, 8, 9, 10)    # SCL: cloud shadow, cloud medium/high, thin cirrus
CHECK_BATCH = 8                  # passes cloud-checked in parallel per round
COND_TTL = 6 * 3600
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_MAX_RETRY=2, GDAL_CACHEMAX=64)
ATTRIBUTION = "Contains modified Copernicus Sentinel data; conditions: Open-Meteo (model hindcast)"

_session = requests.Session()
_lock = threading.Lock()
_items: dict = {}                # item id -> item summary (never changes)
_cloud: dict = {}                # (item id, lat, lon, size) -> (cloud_frac, nodata_frac)
_cond: dict = {}                 # (lat, lon) -> (time, hourly dict)
_img: "OrderedDict[tuple, bytes]" = OrderedDict()
IMG_CACHE = 64


class HistoryError(Exception):
    """Imagery catalogue or files unreachable."""


# ── catalogue ────────────────────────────────────────────────────

def _summary(f: dict) -> Optional[dict]:
    a, p = f.get("assets", {}), f.get("properties", {})
    tci, scl = (a.get("visual") or {}).get("href"), (a.get("scl") or {}).get("href")
    if not (tci and scl and tci.startswith(COG_HOST) and scl.startswith(COG_HOST)):
        return None
    return {"id": f["id"], "time": p.get("datetime"), "satellite": p.get("platform"),
            "tile_cloud_pct": p.get("eo:cloud_cover"), "tci": tci, "scl": scl}


def search(lat: float, lon: float, days: int, max_tile_cloud: float = 90) -> list[dict]:
    """Sentinel-2 passes over the point in the last `days`, newest first."""
    end = datetime.now(timezone.utc)
    body = {"collections": [COLLECTION], "bbox": [lon - 0.005, lat - 0.005, lon + 0.005, lat + 0.005],
            "datetime": f"{(end - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "query": {"eo:cloud_cover": {"lte": max_tile_cloud}}, "limit": 200,
            "fields": {"include": ["id", "properties.datetime", "properties.platform", "properties.eo:cloud_cover",
                                   "assets.visual.href", "assets.scl.href"], "exclude": ["links", "geometry"]}}
    try:
        r = _session.post(f"{STAC}/search", json=body, timeout=20)
        r.raise_for_status()
        feats = r.json().get("features", [])
    except Exception as e:
        raise HistoryError(f"Sentinel-2 catalogue: {type(e).__name__}: {e}") from e
    out = [s for s in map(_summary, feats) if s]
    with _lock:
        for s in out:
            _items[s["id"]] = s
    return sorted(out, key=lambda s: s["time"] or "", reverse=True)


def item(item_id: str) -> dict:
    if not ITEM_ID.match(item_id):
        raise ValueError("Not a Sentinel-2 L2A item id")
    with _lock:
        hit = _items.get(item_id)
    if hit:
        return hit
    try:
        r = _session.get(f"{STAC}/collections/{COLLECTION}/items/{item_id}", timeout=15)
    except Exception as e:
        raise HistoryError(f"Sentinel-2 catalogue: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        raise ValueError(f"No Sentinel-2 item {item_id!r}")
    if r.status_code != 200:
        raise HistoryError(f"Sentinel-2 catalogue: HTTP {r.status_code}")
    s = _summary(r.json())
    if not s:
        raise ValueError(f"Item {item_id!r} has no usable imagery")
    with _lock:
        _items[item_id] = s
    return s


# ── raster windows (rasterio imported lazily: only these endpoints pay for GDAL) ─────

def _window(href: str, lat: float, lon: float, size_m: float):
    import rasterio
    from rasterio.warp import transform
    from rasterio.windows import from_bounds
    half = size_m / 2
    try:
        with rasterio.Env(**GDAL_ENV), rasterio.open(href) as src:
            xs, ys = transform("EPSG:4326", src.crs, [lon], [lat])
            w = from_bounds(xs[0] - half, ys[0] - half, xs[0] + half, ys[0] + half, src.transform)
            return src.read(window=w, boundless=True, fill_value=0)
    except Exception as e:
        raise HistoryError(f"Sentinel-2 file: {type(e).__name__}: {e}") from e


def spot_cloud(s: dict, lat: float, lon: float, size_m: float) -> tuple[float, float]:
    """(cloud fraction, no-data fraction) inside the crop, from the scene classification layer."""
    key = (s["id"], round(lat, 4), round(lon, 4), round(size_m))
    with _lock:
        if key in _cloud:
            return _cloud[key]
    import numpy as np
    a = _window(s["scl"], lat, lon, size_m)[0]
    n = a.size or 1
    res = (float(np.isin(a, CLOUD_CLASSES).sum()) / n, float((a == 0).sum()) / n)
    with _lock:
        _cloud[key] = res
    return res


def image_jpeg(item_id: str, lat: float, lon: float, size_m: float = 2400, px: int = 720) -> bytes:
    """True-colour crop centred on the point, brightened for screens, as JPEG."""
    key = (item_id, round(lat, 4), round(lon, 4), round(size_m), px)
    with _lock:
        if key in _img:
            _img.move_to_end(key)
            return _img[key]
    import numpy as np
    from PIL import Image
    a = _window(item(item_id)["tci"], lat, lon, size_m).astype("float32") / 255.0
    a = np.clip((a * 1.7) ** 0.85, 0, 1)            # TCI is dark over water; same curve for every image
    img = Image.fromarray((a.transpose(1, 2, 0) * 255).astype("uint8"))
    img = img.resize((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True)
    out = buf.getvalue()
    with _lock:
        _img[key] = out
        while len(_img) > IMG_CACHE:
            _img.popitem(last=False)
    return out


# ── conditions ───────────────────────────────────────────────────

def conditions(lat: float, lon: float) -> dict:
    """UTC hour 'YYYY-MM-DDTHH' -> modelled swell, wind and tide, for the last MAX_DAYS days."""
    key = (round(lat, 3), round(lon, 3))
    with _lock:
        hit = _cond.get(key)
    if hit and time.monotonic() - hit[0] < COND_TTL:
        return hit[1]
    common = {"latitude": lat, "longitude": lon, "timezone": "GMT", "past_days": MAX_DAYS, "forecast_days": 1}
    m = surfdeck.om_get("marine", {**common, "cell_selection": "sea", "hourly": [
        "wave_height", "swell_wave_height", "swell_wave_direction", "swell_wave_period",
        "swell_wave_peak_period", "wind_wave_height", "sea_level_height_msl"]}, "marine history")
    w = surfdeck.om_get("weather", {**common, "wind_speed_unit": "mph",
                                    "hourly": ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]},
                        "weather history")
    mh, wh = m["hourly"], w["hourly"]
    w_idx = {t: i for i, t in enumerate(wh["time"])}
    g = lambda arr, i: arr[i] if arr is not None and i is not None and i < len(arr) else None
    rows = [{"time": t + ":00+00:00", "sea_level_m": g(mh.get("sea_level_height_msl"), i)}
            for i, t in enumerate(mh["time"])]
    tides = (surfdeck.tides_from_sea_level(rows, timezone.utc) or {}).get("hourly", {})
    out = {}
    for i, t in enumerate(mh["time"]):
        j = w_idx.get(t)
        h = t[:13]
        period, _ = surfdeck.peak_period(g(mh.get("swell_wave_period"), i), g(mh.get("swell_wave_direction"), i),
                                         g(mh.get("swell_wave_peak_period"), i), None, None)
        sd = g(mh.get("swell_wave_direction"), i)
        wd = g(wh.get("wind_direction_10m"), j)
        tide = tides.get(h)
        out[h] = {
            "wave_height_m": g(mh.get("wave_height"), i),
            "swell_height_m": g(mh.get("swell_wave_height"), i),
            "swell_period_s": round(period, 1) if period is not None else None,
            "swell_direction_deg": sd,
            "swell_direction": surfdeck.deg_to_compass(sd) if sd is not None else None,
            "wind_wave_height_m": g(mh.get("wind_wave_height"), i),
            "wind_speed_mph": g(wh.get("wind_speed_10m"), j),
            "wind_gusts_mph": g(wh.get("wind_gusts_10m"), j),
            "wind_direction_deg": wd,
            "wind_direction": surfdeck.deg_to_compass(wd) if wd is not None else None,
            "tide_height_m": tide["height_m"] if tide else None,
            "tide_level": tide["level"] if tide else None,
            "tide_status": tide["status"] if tide else None,
        }
    with _lock:
        _cond[key] = (time.monotonic(), out)
    return out


def _hour_key(iso: str) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (t + timedelta(minutes=30)).strftime("%Y-%m-%dT%H")       # nearest hour


# ── the whole thing ──────────────────────────────────────────────

def history(lat: float, lon: float, days: int = 60, max_cloud: float = 20, limit: int = 12,
            size_m: float = 2400, include_cloudy: bool = False) -> dict:
    """Clear passes over the spot, newest first, each with the conditions at that hour."""
    days = min(days, MAX_DAYS)
    found = search(lat, lon, days)
    notes = []

    # One image per overpass: where tiles overlap the same pass shows up twice.
    passes, seen, checked = [], set(), 0
    i = 0
    with ThreadPoolExecutor(CHECK_BATCH) as ex:
        while i < len(found) and len(passes) < limit:
            batch = found[i:i + CHECK_BATCH]
            i += CHECK_BATCH
            clouds = list(ex.map(lambda s: _safe_cloud(s, lat, lon, size_m), batch))
            for s, c in zip(batch, clouds):
                checked += 1
                when = (s["time"] or "")[:13]
                if c is None or when in seen:
                    continue
                cloud, nodata = c
                if nodata > 0.2:
                    continue                                   # spot on the tile edge; the other tile has it
                if cloud * 100 > max_cloud and not include_cloudy:
                    continue
                seen.add(when)
                passes.append({**{k: s[k] for k in ("id", "time", "satellite", "tile_cloud_pct")},
                               "spot_cloud_pct": round(cloud * 100)})
                if len(passes) >= limit:
                    break

    try:
        cond = conditions(lat, lon)
    except surfdeck.UpstreamError as e:
        cond = {}
        notes.append(f"Conditions unavailable right now ({e}); images still shown.")
    for p in passes:
        p["conditions"] = cond.get(_hour_key(p["time"])) if p["time"] else None

    if not passes:
        notes.append(f"No passes under {max_cloud:.0f}% cloud at the spot in the last {days} days. "
                     "Try a higher max_cloud or more days.")
    return {"lat": lat, "lon": lon, "days": days, "size_m": size_m, "max_cloud": max_cloud,
            "passes_found": len(found), "passes_checked": checked, "count": len(passes), "passes": passes,
            "resolution_m": 10, "notes": notes, "attribution": ATTRIBUTION}


def _safe_cloud(s, lat, lon, size_m):
    try:
        return spot_cloud(s, lat, lon, size_m)
    except HistoryError:
        return None
