"""Swell field over a region, for the stylised swell map.

Open-Meteo's marine API takes comma-separated latitude/longitude lists, so a
whole grid comes back in a few requests (batched to keep URLs short). Land
points come back null, which doubles as a coarse coastline. Clients draw it as
streamlines, one frame per 3 or 6 hours, in the style of scripts/ocean_swell_map.py.
"""
import time
from datetime import datetime, timezone

import surfdeck
from surfdeck import ISLANDS, UpstreamError

# Open-Meteo counts every location in a multi-location request as one call
# (confirmed: a 400 + 400 point pair tripped the 600/minute free limit). Free
# tier: 600/min, 10,000/day. The map is stylised: it only needs to show a swell
# system coming, so grids are coarse and rebuilt every 6h (the build reuses the
# published copy in between). Daily cost ~ (140 + 63 + 468) x 4 ~ 2,700 calls.
MAP_REGIONS = {
    "bermuda": {"name": "Bermuda", "lat": (24.0, 42.0), "lon": (-82.0, -56.0), "step": 2.0,
                "markers": ["bermuda"], "timezone": "Atlantic/Bermuda", "every_h": 6},
    "newquay": {"name": "Newquay, Cornwall", "lat": (44.0, 56.0), "lon": (-16.0, 0.0), "step": 2.0,
                "markers": ["newquay"], "timezone": "Europe/London", "every_h": 6},
    "global":  {"name": "Global", "lat": (-60.0, 60.0), "lon": (-180.0, 170.0), "step": 10.0,
                "markers": list(ISLANDS), "timezone": "UTC", "every_h": 6},
}
MAP_HOURS = 48
MAP_STEP_H = 3          # frames every 3h; clients show every 2nd frame for 6h steps
BATCH = 500             # locations per request (each counts as a call)
BATCH_PAUSE_S = 62      # between batches, to stay under 600 calls/minute


def _frange(a: float, b: float, step: float) -> list[float]:
    n = int(round((b - a) / step))
    return [round(a + i * step, 3) for i in range(n + 1)]


def _fetch_batch(pts: list[tuple[float, float]], hours: int) -> list[dict]:
    body = surfdeck.om_get("marine", {
        "latitude":  ",".join(str(la) for la, _ in pts),
        "longitude": ",".join(str(lo) for _, lo in pts),
        "hourly": "swell_wave_height,swell_wave_direction,swell_wave_period",
        "forecast_hours": hours + 1, "timezone": "GMT", "cell_selection": "nearest",
    }, "map", timeout=max(surfdeck.OPEN_METEO_TIMEOUT, 60))
    if isinstance(body, dict) and "hourly" in body:
        body = [body]                       # single location comes back as an object
    if not isinstance(body, list) or len(body) != len(pts):
        raise UpstreamError(f"map: expected {len(pts)} points, got {len(body) if isinstance(body, list) else 'an object'}")
    return body


def fetch_swell_grid(region_key: str, hours: int = MAP_HOURS) -> dict:
    """Returns {lats, lons, times, h, d, p, markers, ...}; h/d/p are [frame][lat][lon],
    None on land. Frames are every MAP_STEP_H hours from the current hour.

    Raises KeyError for an unknown region, UpstreamError if Open-Meteo fails.
    """
    region = MAP_REGIONS[region_key]
    lats = _frange(*region["lat"], region["step"])
    lons = _frange(*region["lon"], region["step"])
    pts = [(la, lo) for la in lats for lo in lons]

    body: list[dict] = []
    for i in range(0, len(pts), BATCH):
        if i:
            time.sleep(BATCH_PAUSE_S)
        body.extend(_fetch_batch(pts[i:i + BATCH], hours))

    all_times = [int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp())
                 for t in body[0]["hourly"]["time"]]
    keep = list(range(0, len(all_times), MAP_STEP_H))
    times = [all_times[i] for i in keep]
    nt, ny, nx = len(times), len(lats), len(lons)

    def grid(var: str, nd):
        out = [[[None] * nx for _ in range(ny)] for _ in range(nt)]
        for k, pt in enumerate(body):
            y, x = divmod(k, nx)
            vals = pt["hourly"][var]
            for t, src in enumerate(keep):
                v = vals[src] if src < len(vals) else None
                if v is not None:
                    out[t][y][x] = round(v, nd) if nd else int(round(v))
        return out

    markers = [{"lat": ISLANDS[k][0].offshore_lat, "lon": ISLANDS[k][0].offshore_lon,
                "name": ISLANDS[k][0].name.split(",")[0]} for k in region["markers"]]
    return {
        "region": region_key, "island": region["name"], "island_key": region_key,
        "timezone": region["timezone"], "step_h": MAP_STEP_H, "step_deg": region["step"],
        "generated": int(datetime.now(timezone.utc).timestamp()),
        "marker": markers[0], "markers": markers,
        "lats": lats, "lons": lons, "times": times,
        "h": grid("swell_wave_height", 2),
        "d": grid("swell_wave_direction", None),
        "p": grid("swell_wave_period", 1),
    }
