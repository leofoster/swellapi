"""Swell field over a region, for the stylised swell map.

One Open-Meteo marine request covers the whole grid (latitude/longitude accept
comma-separated lists). Land points come back as null, which doubles as a
coarse coastline. Clients draw it as streamlines, one frame per 3 or 6 hours,
in the style of scripts/ocean_swell_map.py.
"""
from datetime import datetime, timezone
from typing import Optional

import surfdeck
from surfdeck import ISLANDS, UpstreamError

# Bounding boxes and grid steps. ~200 points each: Open-Meteo may count each
# location against the free daily quota, so keep grids coarse (unverified).
MAP_REGIONS = {
    "bermuda": {"lat": (24.0, 42.0), "lon": (-82.0, -56.0), "step": 1.5},
    "newquay": {"lat": (44.0, 56.0), "lon": (-16.0, 0.0),   "step": 1.0},
}
MAP_HOURS = 48
MAP_STEP_H = 3      # frames every 3h; clients can show every 2nd frame for 6h steps


def _frange(a: float, b: float, step: float) -> list[float]:
    n = int(round((b - a) / step))
    return [round(a + i * step, 3) for i in range(n + 1)]


def fetch_swell_grid(island_key: str, hours: int = MAP_HOURS) -> dict:
    """Returns {lats, lons, times, h, d, p, ...}; h/d/p are [frame][lat][lon], None on land.
    Frames are every MAP_STEP_H hours from the current hour, MAP_HOURS ahead.

    Raises KeyError for an island with no map region, UpstreamError if Open-Meteo fails.
    """
    region = MAP_REGIONS[island_key]
    island, _ = ISLANDS[island_key]
    lats = _frange(*region["lat"], region["step"])
    lons = _frange(*region["lon"], region["step"])
    pts = [(la, lo) for la in lats for lo in lons]

    try:
        resp = surfdeck._session.get(surfdeck.MARINE_URL, params={
            "latitude":  ",".join(str(la) for la, _ in pts),
            "longitude": ",".join(str(lo) for _, lo in pts),
            "hourly": "swell_wave_height,swell_wave_direction,swell_wave_period",
            "forecast_hours": hours + 1, "timezone": "GMT", "cell_selection": "nearest",
        }, headers=surfdeck._PROXY_HEADERS, timeout=surfdeck.OPEN_METEO_TIMEOUT)
        body = resp.json()
    except Exception as e:
        raise UpstreamError(f"map: {type(e).__name__}: {e}") from e
    if resp.status_code != 200 or isinstance(body, dict):
        reason = body.get("reason") if isinstance(body, dict) else None
        raise UpstreamError(f"map: HTTP {resp.status_code}: {reason}")
    if len(body) != len(pts):
        raise UpstreamError(f"map: expected {len(pts)} points, got {len(body)}")

    all_times = [int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp())
                 for t in body[0]["hourly"]["time"]]
    keep = list(range(0, len(all_times), MAP_STEP_H))      # 3-hourly frames
    times = [all_times[i] for i in keep]
    nt, ny, nx = len(times), len(lats), len(lons)

    def grid(var: str, nd: Optional[int]):
        out = [[[None] * nx for _ in range(ny)] for _ in range(nt)]
        for k, pt in enumerate(body):
            y, x = divmod(k, nx)
            vals = pt["hourly"][var]
            for t, src in enumerate(keep):
                v = vals[src] if src < len(vals) else None
                if v is not None:
                    out[t][y][x] = round(v, nd) if nd else int(round(v))
        return out

    return {
        "island": island.name, "island_key": island_key, "timezone": island.timezone,
        "step_h": MAP_STEP_H,
        "generated": int(datetime.now(timezone.utc).timestamp()),
        "marker": {"lat": island.offshore_lat, "lon": island.offshore_lon, "name": island.name},
        "lats": lats, "lons": lons, "times": times,
        "h": grid("swell_wave_height", 2),
        "d": grid("swell_wave_direction", None),
        "p": grid("swell_wave_period", 1),
    }
