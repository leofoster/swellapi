"""
Island Surf Forecast
─────────────────────────────────────────────────────────────────
Architecture:
  1. Fetch one central offshore position (the island's open ocean reading)
  2. Pull nearest NOAA buoy as ground truth
  3. Cross-reference both — build a confidence score
  4. Infer conditions for each shore from the single dataset
  5. Each SpotConfig is just a directional filter + local modifier
"""

import math
import os
import threading
import time
import requests
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo
import statistics

# ─────────────────────────────────────────────────────────────────
#  UPSTREAM ROUTING
# ─────────────────────────────────────────────────────────────────
# Open-Meteo's free tier limits requests per IP per day. Render's free tier
# shares outbound IPs with other customers, so that budget is often gone
# before we use it (429 "Daily API request limit exceeded").
#
# Optional relay (api_prox.py on a home connection, exposed by a tunnel):
#   OPEN_METEO_PROXY  fixed relay base URL (simple; local dev uses this)
#   RELAY_CONFIG_URL  URL of a small JSON switch, {"enabled": bool, "url": "..."},
#                     re-read every RELAY_CONFIG_TTL seconds. scripts/relay.py keeps
#                     it current in a GitHub gist, so the relay can be turned on/off
#                     (and its tunnel URL can change) without touching Render.
#   PROXY_TOKEN       shared secret the relay requires (X-Proxy-Token)
# If the relay fails, requests fall back to calling Open-Meteo directly.
def _env(name: str) -> str:
    """Env var with pasting accidents removed: whitespace, newlines, straight or curly quotes."""
    return os.environ.get(name, "").strip().strip("\"'\u201c\u201d\u2018\u2019").strip()


OPEN_METEO_PROXY   = _env("OPEN_METEO_PROXY").rstrip("/")
RELAY_CONFIG_URL   = _env("RELAY_CONFIG_URL")
RELAY_CONFIG_TTL   = float(os.environ.get("RELAY_CONFIG_TTL", "60"))
PROXY_TOKEN        = _env("PROXY_TOKEN")
OPEN_METEO_TIMEOUT = float(os.environ.get("OPEN_METEO_TIMEOUT", "20"))
# Open-Meteo refreshes hourly, so re-asking more often only burns the quota.
OPEN_METEO_CACHE_TTL = float(os.environ.get("OPEN_METEO_CACHE_TTL", "1800"))
# Tides: "model" = Open-Meteo sea_level_height_msl (includes tides; a coarse ocean
# model, not harbour tide tables), "mock" = synthetic sine (testing only), "off".
MOCK_TIDES = os.environ.get("MOCK_TIDES", "") == "1"
TIDES = "mock" if MOCK_TIDES else os.environ.get("TIDES", "model")

DIRECT_URLS = {"marine": "https://marine-api.open-meteo.com/v1/marine",
               "weather": "https://api.open-meteo.com/v1/forecast"}
RELAY_PATHS = {"marine": "/marine", "weather": "/forecast"}


class UpstreamError(RuntimeError):
    """Open-Meteo could not be reached or refused the request. Message is the reason."""


class NoCoverageError(ValueError):
    """The location is too far from any ocean model data to forecast surf (e.g. inland)."""


MAX_GRID_KM = float(os.environ.get("MAX_GRID_KM", "100"))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def safe_mean(values: list) -> Optional[float]:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return statistics.mean(clean)

@dataclass
class Island:
    name: str
    offshore_lat: float
    offshore_lon: float
    buoy_ids: list[str]
    timezone: str = "UTC"

@dataclass
class SpotConfig:
    name: str
    shore: str

    swell_dir_min: float
    swell_dir_max: float
    swell_dir_ideal: float

    swell_period_min: float
    swell_period_ideal: float

    swell_height_min: float
    swell_height_ideal: float
    swell_height_max: float

    wind_offshore_min: float
    wind_offshore_max: float
    wind_speed_max: float

    break_type: str
    exposure: float = 1.0
    shadow_zones: list = field(default_factory=list)
    wrap_factor: float = 0.0
    height_scale: float = 1.0   # per-spot calibration of surf height (e.g. 0.8 if it reads high)

    # ── New Tide Preferences ───────────────────────────────────────
    tide_level_ideal: Optional[str] = None      # "low", "low-mid", "mid", "mid-high", "high"
    tide_movement_ideal: Optional[str] = None   # "rising", "falling"

    notes: str = ""

ISLANDS: dict[str, tuple[Island, dict[str, SpotConfig]]] = {
    "bermuda": (
        Island(
            name="Bermuda",
            offshore_lat=32.30,
            offshore_lon=-64.80,
            buoy_ids=["41049"],
            timezone="Atlantic/Bermuda",
        ),
        {
            "south_shore": SpotConfig(
                name="Southlands", shore="south",
                swell_dir_min=135, swell_dir_max=260, swell_dir_ideal=180,
                swell_period_min=10, swell_period_ideal=16,
                swell_height_min=0.8, swell_height_ideal=2.0, swell_height_max=4.5,
                wind_offshore_min=315, wind_offshore_max=60, wind_speed_max=20,
                break_type="reef", exposure=1.0, shadow_zones=[(260, 315)], wrap_factor=0.2,
                tide_level_ideal="mid-high", tide_movement_ideal="rising", # Example tide config
                notes="Best surf in Bermuda. Needs S or SSW groundswell.",
            ),
            "north_shore": SpotConfig(
                name="North Rock", shore="north",
                swell_dir_min=300, swell_dir_max=60, swell_dir_ideal=350,
                swell_period_min=9, swell_period_ideal=14,
                swell_height_min=0.6, swell_height_ideal=1.5, swell_height_max=3.0,
                wind_offshore_min=135, wind_offshore_max=225, wind_speed_max=18,
                break_type="reef", exposure=0.8, shadow_zones=[(60, 300)], wrap_factor=0.15,
                notes="Works winter N Atlantic swells. More sheltered than south shore.",
            ),
        }
    ),
    "newquay": (
        Island(
            name="Newquay, Cornwall",
            offshore_lat=50.35, offshore_lon=-5.30, buoy_ids=["62029", "62105"], timezone="Europe/London",
        ),
        {
            "fistral": SpotConfig(
                name="Fistral Beach", shore="west",
                swell_dir_min=180, swell_dir_max=315, swell_dir_ideal=250,
                swell_period_min=8, swell_period_ideal=14,
                swell_height_min=0.5, swell_height_ideal=1.5, swell_height_max=3.5,
                wind_offshore_min=45, wind_offshore_max=135, wind_speed_max=20,
                break_type="beach", exposure=1.0, shadow_zones=[(315, 180)],
                tide_level_ideal="mid", # Example tide config
                notes="Picks up all W and SW Atlantic swell. Most consistent beach in Cornwall.",
            ),
        }
    ),
}

# ─────────────────────────────────────────────────────────────────
#  DATA FETCHING
# ─────────────────────────────────────────────────────────────────

_session = requests.Session()
_cache: dict[tuple, tuple[float, list, str]] = {}
_cache_lock = threading.Lock()
FORECAST_DAYS = 7   # always fetch the full window; callers slice, so one cache entry serves all


_relay = {"checked": -1e9, "url": None}
_relay_lock = threading.Lock()


def relay_base() -> Optional[str]:
    """Current relay base URL, or None for direct. Reads the switch at most every RELAY_CONFIG_TTL s;
    if the switch can't be read, keeps the last known answer."""
    if OPEN_METEO_PROXY:
        return OPEN_METEO_PROXY
    if not RELAY_CONFIG_URL:
        return None
    with _relay_lock:
        if time.monotonic() - _relay["checked"] < RELAY_CONFIG_TTL:
            return _relay["url"]
        try:
            cfg = _session.get(RELAY_CONFIG_URL, timeout=5).json()
            url = (cfg.get("url") or "").rstrip("/") if cfg.get("enabled") else ""
            _relay["url"] = url or None
        except Exception:
            pass
        _relay["checked"] = time.monotonic()
        return _relay["url"]


def _request(url: str, params: dict, label: str, headers: dict, timeout: float):
    try:
        resp = _session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise UpstreamError(f"{label}: {type(e).__name__}: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        raise UpstreamError(f"{label}: HTTP {resp.status_code}, non-JSON body: {resp.text[:120]!r}")
    if resp.status_code != 200 or (isinstance(body, dict) and body.get("error")):
        reason = body.get("reason") if isinstance(body, dict) else None
        raise UpstreamError(f"{label}: HTTP {resp.status_code}: {reason or 'error'}")
    return body


def om_get(kind: str, params: dict, label: str, timeout: Optional[float] = None):
    """GET an Open-Meteo API ("marine" or "weather"), via the relay when it is on.
    Any relay failure falls back to a direct call. Returns parsed JSON; raises UpstreamError."""
    timeout = timeout or OPEN_METEO_TIMEOUT
    base = relay_base()
    if base:
        headers = {"ngrok-skip-browser-warning": "true"}
        if PROXY_TOKEN:
            headers["X-Proxy-Token"] = PROXY_TOKEN
        try:
            return _request(base + RELAY_PATHS[kind], params, f"{label} (relay)", headers, timeout)
        except Exception as e:                # anything wrong with the relay (or its settings): go direct
            print(f"  relay failed, going direct: {type(e).__name__}: {e}")
    return _request(DIRECT_URLS[kind], params, label, {}, timeout)


def _get_json(kind: str, params: dict, label: str) -> dict:
    body = om_get(kind, params, label)
    if not isinstance(body, dict) or "hourly" not in body:
        raise UpstreamError(f"{label}: no hourly data (no model coverage here?)")
    return body


def fetch_forecast(lat: float, lon: float) -> tuple[list[dict], str]:
    """Hourly marine + wind data for the next FORECAST_DAYS days, plus the
    location's IANA timezone as Open-Meteo resolves it (e.g. "Atlantic/Bermuda";
    open-ocean points can come back as "GMT").

    Row times are timezone-aware UTC ISO strings. Raises UpstreamError.
    Cached for OPEN_METEO_CACHE_TTL seconds per location.
    """
    key = (round(lat, 3), round(lon, 3))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < OPEN_METEO_CACHE_TTL:
            if hit[1] is None:
                raise NoCoverageError(hit[2])
            return hit[1], hit[2]

    common = {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": FORECAST_DAYS}
    # cell_selection=sea: a pin on the beach snaps to the nearest sea cell instead of a
    # land cell with no wave data. The distance check below refuses inland points.
    marine = _get_json("marine", {**common, "cell_selection": "sea", "hourly": [
        "wave_height", "wave_direction", "wave_period", "swell_wave_height",
        "swell_wave_direction", "swell_wave_period", "swell_wave_peak_period",
        "wind_wave_height", "wind_wave_period",
        "secondary_swell_wave_height", "secondary_swell_wave_direction", "secondary_swell_wave_period",
        "sea_level_height_msl",
    ]}, "marine")
    weather = _get_json("weather", {**common, "wind_speed_unit": "mph",
        "hourly": ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"],
        "daily": ["sunrise", "sunset"]}, "weather")

    # Open-Meteo snaps to the nearest ocean grid cell, which for an inland point can be
    # hundreds of km away. Refuse rather than forecast someone else's coast.
    grid_km = haversine_km(lat, lon, marine.get("latitude", lat), marine.get("longitude", lon))
    if grid_km > MAX_GRID_KM:
        msg = (f"No ocean model data within {MAX_GRID_KM:.0f} km of ({lat}, {lon}); nearest is "
               f"{grid_km:.0f} km away. Pick a point on or near the coast.")
        with _cache_lock:
            _cache[key] = (now, None, msg)
        raise NoCoverageError(msg)

    # Peak periods. Open-Meteo's default swell_wave_period is a MEAN period; surfers (and
    # Surfline, and buoys' "dominant period") use the PEAK period, typically 3-5 s longer for
    # groundswell. Native peak period only exists in some regions, so NOAA GFS-Wave's swell
    # periods are fetched too (they track buoy peak periods closely). Optional: on failure the
    # mean x PEAK_RATIO estimate is used.
    try:
        gfs = _get_json("marine", {**common, "cell_selection": "sea", "models": "ncep_gfswave025", "hourly": [
            "swell_wave_period", "swell_wave_direction", "secondary_swell_wave_period", "secondary_swell_wave_direction",
        ]}, "marine (gfs periods)")["hourly"]
    except UpstreamError:
        gfs = None

    tz_name = weather.get("timezone") or marine.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name, tz = "UTC", timezone.utc

    def to_utc(t: str) -> str:
        return datetime.fromisoformat(t).replace(tzinfo=tz).astimezone(timezone.utc).isoformat()

    mh, wh = marine["hourly"], weather["hourly"]
    g = lambda arr, i: arr[i] if arr is not None and i is not None and i < len(arr) else None

    # Daylight windows (UTC): first light ~30 min before sunrise, last light at sunset.
    daily = weather.get("daily") or {}
    light = []
    for rise, set_ in zip(daily.get("sunrise") or [], daily.get("sunset") or []):
        if rise and set_:
            light.append((datetime.fromisoformat(rise).replace(tzinfo=tz) - timedelta(minutes=30),
                          datetime.fromisoformat(set_).replace(tzinfo=tz)))

    def is_daylight(t_local: datetime) -> Optional[bool]:
        if not light:
            return None
        return any(a <= t_local < b for a, b in light)

    # Align on timestamp rather than index, in case the two models differ in length.
    w_idx = {t: i for i, t in enumerate(wh["time"])}
    g_idx = {t: i for i, t in enumerate(gfs["time"])} if gfs else {}
    rows = []
    for i, t in enumerate(mh["time"]):
        j = w_idx.get(t)
        gi = g_idx.get(t)
        t_local = datetime.fromisoformat(t).replace(tzinfo=tz)
        p1, src1 = peak_period(mh["swell_wave_period"][i], mh["swell_wave_direction"][i], mh["swell_wave_peak_period"][i],
                               g(gfs.get("swell_wave_period"), gi) if gfs else None,
                               g(gfs.get("swell_wave_direction"), gi) if gfs else None)
        p2, _ = peak_period(g(mh.get("secondary_swell_wave_period"), i), g(mh.get("secondary_swell_wave_direction"), i), None,
                            g(gfs.get("secondary_swell_wave_period"), gi) if gfs else None,
                            g(gfs.get("secondary_swell_wave_direction"), gi) if gfs else None)
        rows.append({
            "time":                to_utc(t),
            "swell_height_m":      mh["swell_wave_height"][i],
            "swell_direction_deg": mh["swell_wave_direction"][i],
            "swell_period_s":      mh["swell_wave_period"][i],
            "swell_peak_period_s": mh["swell_wave_peak_period"][i],
            "wave_height_m":       mh["wave_height"][i],
            "wind_wave_height_m":  mh["wind_wave_height"][i],
            "wind_wave_period_s":  mh["wind_wave_period"][i],
            "wind_speed_mph":      wh["wind_speed_10m"][j] if j is not None else None,
            "wind_direction_deg":  wh["wind_direction_10m"][j] if j is not None else None,
            "wind_gusts_mph":      wh["wind_gusts_10m"][j] if j is not None else None,
            "swell_peak_period_s": p1,
            "period_source":       src1,
            "swell2_peak_period_s": p2,
            "swell2_height_m":     g(mh.get("secondary_swell_wave_height"), i),
            "swell2_direction_deg": g(mh.get("secondary_swell_wave_direction"), i),
            "swell2_period_s":     g(mh.get("secondary_swell_wave_period"), i),
            "sea_level_m":         g(mh.get("sea_level_height_msl"), i),
            # end of the hour counts, so a 06:00-07:00 slot with first light at 06:20 is daylight
            "daylight":            is_daylight(t_local + timedelta(minutes=59)),
            "grid_km":             round(grid_km, 1),
        })

    # Inland (or a land cell) comes back with every wave value null.
    if not any(r["swell_height_m"] is not None or r["wave_height_m"] is not None for r in rows):
        msg = (f"No ocean wave data at ({lat}, {lon}): the model has this as land. "
               f"Move the pin onto the water just off the break.")
        with _cache_lock:
            _cache[key] = (now, None, msg)
        raise NoCoverageError(msg)

    with _cache_lock:
        _cache[key] = (now, rows, tz_name)
    return rows, tz_name


PEAK_RATIO = 1.35   # buoy peak / model mean period, measured at NOAA buoys 2026-11-16 (14-17 s swell)


def peak_period(mean: Optional[float], direction: Optional[float], native_peak: Optional[float],
                gfs_period: Optional[float], gfs_dir: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    """Best available peak period for one swell train, and where it came from."""
    if native_peak and 3 < native_peak < 25:
        return round(native_peak, 1), "model-peak"
    if (gfs_period and 3 < gfs_period < 25 and mean and gfs_period >= mean * 0.9
            and direction is not None and gfs_dir is not None and ang_diff(direction, gfs_dir) <= 45):
        return round(gfs_period, 1), "gfs-wave"          # same swell (direction agrees), peak-like period
    if mean:
        return round(mean * PEAK_RATIO, 1), "estimated"
    return None, None


def fetch_open_meteo(lat: float, lon: float) -> list[dict]:
    """Rows only. See fetch_forecast."""
    return fetch_forecast(lat, lon)[0]


def get_tides(lat: float, lon: float, days: int, now: datetime,
              rows: Optional[list] = None, tz=None) -> Optional[dict]:
    """Tide data for scoring and display, or None. See TIDES."""
    if TIDES == "mock":
        return fetch_mock_tides(lat, lon, days, now)
    if TIDES == "model" and rows:
        return tides_from_sea_level(rows, tz or timezone.utc)
    return None


def tides_from_sea_level(rows: list, tz) -> Optional[dict]:
    """Hourly height, rising/falling, 0-1 level within the local tidal range, and
    daily highs/lows (parabolic peak fit) from Open-Meteo's modelled sea level."""
    pts = [(datetime.fromisoformat(r["time"]), r.get("sea_level_m")) for r in rows]
    pts = [(t, h) for t, h in pts if h is not None]
    if len(pts) < 24:
        return None
    hs = [h for _, h in pts]
    hourly, extremes = {}, {}
    for i, (t, h) in enumerate(pts):
        lo_i, hi_i = max(0, i - 13), min(len(pts), i + 14)          # about one tidal day around t
        win = hs[lo_i:hi_i]
        rng = max(win) - min(win)
        level = (h - min(win)) / rng if rng >= 0.05 else 0.5
        prev, nxt = hs[max(0, i - 1)], hs[min(len(hs) - 1, i + 1)]
        hourly[t.strftime("%Y-%m-%dT%H")] = {"height_m": round(h, 2), "status": "rising" if nxt >= prev else "falling",
                                             "level": round(level, 2)}
        if 0 < i < len(hs) - 1:
            a_, b_, c_ = hs[i - 1], h, hs[i + 1]
            kind = "high" if b_ >= a_ and b_ > c_ else "low" if b_ <= a_ and b_ < c_ else None
            if kind:
                den = a_ - 2 * b_ + c_
                off = 0.5 * (a_ - c_) / den if den else 0.0          # hours from t to the true peak
                peak = b_ - 0.25 * (a_ - c_) * off
                lt = (t + timedelta(hours=off)).astimezone(tz)
                extremes.setdefault(str(lt.date()), {"high": [], "low": []})[kind].append(
                    {"time": lt.strftime("%H:%M"), "height_m": round(peak, 2)})
    return {"hourly": hourly, "extremes": extremes, "source": "open-meteo-model"}


def nearest_buoys(lat: float, lon: float, max_km: float = 200.0, k: int = 3, timeout: float = 3.0) -> list[dict]:
    """Up to k closest active NDBC moored buoys within max_km, nearest first, with id, name, owner,
    lat, lon and distance_km. Station list cached 24h."""
    now = time.monotonic()
    if now - _stations["t"] > 86400:
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(_session.get("https://www.ndbc.noaa.gov/activestations.xml", timeout=timeout).content)
            # moored buoys only: coastal "fixed" stations mostly measure tides/met, not waves
            _stations["list"] = [{"id": st.get("id"), "name": st.get("name"), "owner": st.get("owner"),
                                  "lat": float(st.get("lat")), "lon": float(st.get("lon"))}
                                 for st in root.iter("station") if st.get("type") == "buoy"]
            _stations["t"] = now
        except Exception:
            _stations["t"] = now - 86400 + 600                         # retry in 10 min
    near = []
    for st in _stations["list"]:
        d = haversine_km(lat, lon, st["lat"], st["lon"])
        if d <= max_km:
            near.append({**st, "distance_km": round(d)})
    return sorted(near, key=lambda b: b["distance_km"])[:k]


_stations = {"t": -1e9, "list": []}
_buoy_cache: dict = {}


BUOY_BUDGET_S = 4.0   # total time the buoy lookup may add to a forecast


def buoy_correction(rows: list, lat: float, lon: float, now: datetime, horizon_h: float = 12.0):
    """Nudge the next `horizon_h` hours of swell toward what the nearest buoy measures now.
    Works for any coordinate: no buoy in range, NOAA down, or a station without wave data all
    mean "no correction" (rows returned unchanged, info None). Never raises; bounded time."""
    try:
        return _buoy_correction(rows, lat, lon, now, horizon_h)
    except Exception as e:
        print(f"  buoy correction skipped: {type(e).__name__}: {e}")
        return rows, None


def _buoy_correction(rows, lat, lon, now, horizon_h):
    deadline = time.monotonic() + BUOY_BUDGET_S
    st = obs = None
    for cand in nearest_buoys(lat, lon, timeout=min(BUOY_BUDGET_S, 3.0)):
        if time.monotonic() > deadline:
            break
        hit = _buoy_cache.get(cand["id"])
        if not hit or time.monotonic() - hit[0] > (1800 if hit[1] else 86400):   # remember no-wave stations for a day
            hit = (time.monotonic(), fetch_ndbc_buoy(cand["id"], timeout=max(0.5, deadline - time.monotonic())))
            _buoy_cache[cand["id"]] = hit
        if hit[1] and hit[1].get("wave_height_m"):
            st, obs = cand, hit[1]
            break
    if not obs:
        return rows, None
    cur = min(rows, key=lambda r: abs((datetime.fromisoformat(r["time"]) - now).total_seconds()))
    model = cur.get("wave_height_m")
    if not model or model < 0.1:
        return rows, None
    ratio = max(0.6, min(1.6, obs["wave_height_m"] / model))
    out = []
    for r in rows:
        ahead = (datetime.fromisoformat(r["time"]) - now).total_seconds() / 3600
        k = 1 + (ratio - 1) * max(0.0, 1 - max(ahead, 0) / horizon_h) if ahead > -3 else 1.0
        r = dict(r)
        for key in ("swell_height_m", "swell2_height_m"):
            if r.get(key) is not None:
                r[key] = r[key] * k
        out.append(r)
    info = {"station": st["id"], "name": st.get("name"), "lat": st.get("lat"), "lon": st.get("lon"),
            "distance_km": st["distance_km"], "observed_wave_m": obs["wave_height_m"],
            "model_wave_m": round(model, 2), "ratio": round(ratio, 2), "observed_at": obs.get("timestamp"),
            "summary": summarise_buoys([obs])}
    return out, info


def fetch_mock_tides(lat: float, lon: float, days: int, now: datetime) -> dict:
    """
    Mock semi-diurnal tide generator. Open-Meteo does not provide tide highs/lows natively.
    In production, swap this for an API like Stormglass, WorldTides, or NOAA CO-OPS.
    """
    tides_hourly = {}
    extremes = {}
    
    cycle_hours = 12.4206  # M2 tidal constituent
    mean_sea_level = 2.0
    amplitude = 1.5
    
    # Use longitude to create a static phase offset so tides look realistic globally
    phase_offset = lon / 360.0 * cycle_hours
    epoch = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    
    # 1. Hourly Tide Heights
    for h in range((days + 2) * 24):
        dt = now + timedelta(hours=h)
        date_str = str(dt.date())
        if date_str not in extremes:
            extremes[date_str] = {"high": [], "low": []}
            
        hrs_since = (dt - epoch).total_seconds() / 3600.0
        angle = 2 * math.pi * (hrs_since + phase_offset) / cycle_hours
        height = mean_sea_level + amplitude * math.sin(angle)
        
        status = "rising" if math.cos(angle) > 0 else "falling"
        # Key string to match with model hour: "YYYY-MM-DDTHH"
        tides_hourly[dt.strftime("%Y-%m-%dT%H")] = {
            "height_m": round(height, 2),
            "status": status
        }
        
    # 2. Daily Highs and Lows (Analytic)
    start_hrs = (now - epoch).total_seconds() / 3600.0
    end_hrs = start_hrs + (days + 2) * 24
    
    n = math.floor((start_hrs + phase_offset - cycle_hours/4) / cycle_hours)
    
    while True:
        peak_hr = -phase_offset + cycle_hours/4 + n * cycle_hours
        trough_hr = peak_hr + cycle_hours/2
        
        if peak_hr > end_hrs and trough_hr > end_hrs:
            break
            
        if start_hrs <= peak_hr <= end_hrs:
            peak_dt = epoch + timedelta(hours=peak_hr)
            date_str = str(peak_dt.date())
            if date_str in extremes:
                extremes[date_str]["high"].append({
                    "time": peak_dt.strftime("%H:%M"),
                    "height_m": round(mean_sea_level + amplitude, 2)
                })
                
        if start_hrs <= trough_hr <= end_hrs:
            trough_dt = epoch + timedelta(hours=trough_hr)
            date_str = str(trough_dt.date())
            if date_str in extremes:
                extremes[date_str]["low"].append({
                    "time": trough_dt.strftime("%H:%M"),
                    "height_m": round(mean_sea_level - amplitude, 2)
                })
        n += 1

    return {"hourly": tides_hourly, "extremes": extremes}


def fetch_ndbc_buoy(buoy_id: str, timeout: float = 10) -> Optional[dict]:
    url = f"https://www.ndbc.noaa.gov/data/realtime2/{buoy_id}.txt"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        if len(lines) < 3: return None
        headers, values = lines[0].lstrip("#").split(), lines[2].split()

        def get(key, cast=float):
            try:
                idx = headers.index(key)
                v = values[idx]
                return None if v in ("MM", "99.0", "999", "9999") else cast(v)
            except (ValueError, IndexError): return None

        return {
            "buoy_id":       buoy_id,
            "wave_height_m": get("WVHT"),
            "wave_period_s": get("DPD"),
            "wave_dir_deg":  get("MWD"),
            "wind_speed_ms": get("WSPD"),
            "wind_dir_deg":  get("WDIR"),
            "water_temp_c":  get("WTMP"),
            "timestamp":     f"{values[0]}-{values[1]}-{values[2]} {values[3]}:{values[4]}",
        }
    except Exception as e:
        print(f"  buoy {buoy_id} unavailable: {type(e).__name__}")
        return None

def fetch_all_buoys(buoy_ids: list[str]) -> list[dict]:
    if not buoy_ids: return []
    results = []
    for bid in buoy_ids:
        data = fetch_ndbc_buoy(bid)
        if data and data.get("wave_height_m") is not None:
            results.append(data)
    return results

def summarise_buoys(buoys: list[dict]) -> Optional[dict]:
    if not buoys: return None
    n = len(buoys)
    return {
        "count":        n,
        "avg_height_m": safe_mean([b.get("wave_height_m") for b in buoys]),
        "avg_period_s": safe_mean([b.get("wave_period_s")  for b in buoys]),
        "avg_dir_deg":  safe_mean([b.get("wave_dir_deg")   for b in buoys]),
        "source_label": "1 buoy" if n == 1 else f"{n} buoys averaged",
    }


def build_confidence(model_hour: dict, buoy_summary: Optional[dict], hours_from_now: int) -> dict:
    confidence = 1.0
    flags = []
    model_h, model_p = model_hour.get("swell_height_m") or 0, model_hour.get("swell_period_s") or 0

    if hours_from_now > 72:
        confidence -= min(0.35, (hours_from_now - 72) / 72 * 0.35)
        flags.append(f"{hours_from_now // 24}d forecast — directional guidance only")

    if buoy_summary and hours_from_now <= 12:
        buoy_h, buoy_p, src = buoy_summary.get("avg_height_m"), buoy_summary.get("avg_period_s"), buoy_summary["source_label"]
        if buoy_h is not None:
            delta_h = abs(model_h - buoy_h)
            if delta_h > 1.0:
                confidence -= 0.30
                flags.append(f"model {model_h:.1f}m vs {src} {buoy_h:.1f}m — treat with caution")
            elif delta_h > 0.5:
                confidence -= 0.15
            else:
                flags.append(f"{src} confirms model ✓")
        if buoy_p is not None and model_p and abs(model_p - buoy_p) > 3:
            confidence -= 0.15
    elif buoy_summary is None:
        confidence -= 0.10

    if model_p and model_p < 8: confidence -= 0.10
    ww_h = model_hour.get("wind_wave_height_m") or 0
    if ww_h > 0 and model_h > 0 and (ww_h / max(model_h, 0.1)) > 0.7: confidence -= 0.10
    if model_h > 3.0: confidence -= 0.10

    confidence = round(max(0.05, min(1.0, confidence)), 2)
    label = "HIGH" if confidence >= 0.75 else "MEDIUM" if confidence >= 0.50 else "LOW"
    return {"confidence": confidence, "label": label, "flags": flags, "agreement": confidence >= 0.65}


# ─────────────────────────────────────────────────────────────────
#  SPOT INFERENCE ENGINE
# ─────────────────────────────────────────────────────────────────

def direction_in_window(d: float, dmin: float, dmax: float) -> bool:
    if dmin <= dmax: return dmin <= d <= dmax
    return d >= dmin or d <= dmax

def direction_score(d: float, ideal: float, dmin: float, dmax: float) -> float:
    if not direction_in_window(d, dmin, dmax): return 0.0
    diff = abs(d - ideal)
    if diff > 180: diff = 360 - diff
    half = max(abs(ideal - dmin), abs(ideal - dmax), 1)
    return round(max(0.0, 1.0 - diff / half), 3)

def score_period(p: float, pmin: float, pideal: float) -> float:
    if p < pmin: return round(max(0.0, (p / pmin) * 0.4), 3)
    if p >= pideal: return 1.0
    return round(0.4 + 0.6 * ((p - pmin) / (pideal - pmin)), 3)

def score_height(h: float, hmin: float, hideal: float, hmax: float) -> float:
    if h < hmin: return round(max(0.0, h / hmin * 0.3), 3)
    if h > hmax: return 0.0
    if h <= hideal: return round(0.3 + 0.7 * ((h - hmin) / (hideal - hmin)), 3)
    return round(1.0 - 0.5 * ((h - hideal) / max(hmax - hideal, 0.01)), 3)

def score_wind(wdir: float, wspd: float, spot: SpotConfig) -> float:
    if wspd > spot.wind_speed_max: return 0.1
    if direction_in_window(wdir, spot.wind_offshore_min, spot.wind_offshore_max):
        return round(max(0.5, 1.0 - (wspd / spot.wind_speed_max) * 0.5), 3)
    penalty = min(1.0, wspd / 15)
    return round(max(0.0, 0.4 - penalty * 0.4), 3)

def score_tide(tide_height: float, tide_status: str, spot: SpotConfig, level: Optional[float] = None) -> float:
    """Calculates a modifier between -0.15 and +0.15 based on tide preferences.
    `level` (0 = local low, 1 = local high) is used when known; otherwise the
    height is normalised against an assumed 0.5-3.5m range (mock tides)."""
    modifier = 0.0
    norm = level if level is not None else min(max((tide_height - 0.5) / 3.0, 0.0), 1.0)
    
    level_map = {"low": 0.0, "low-mid": 0.25, "mid-low": 0.25, "mid": 0.5,
                 "mid-high": 0.75, "high-mid": 0.75, "high": 1.0}
    
    if spot.tide_level_ideal in level_map:
        ideal_norm = level_map[spot.tide_level_ideal]
        dist = abs(norm - ideal_norm)
        modifier += (0.5 - dist) * 0.2  # Closer to ideal adds +0.10, opposite subtracts -0.10

    if spot.tide_movement_ideal:
        if spot.tide_movement_ideal == tide_status:
            modifier += 0.05
        else:
            modifier -= 0.05
            
    return round(modifier, 3)

def infer_spot_v1(hour: dict, spot: SpotConfig, tide_hour: dict = None) -> dict:
    """Original engine: primary swell only, hard direction window, binary wind."""
    swell_dir = hour.get("swell_direction_deg") or 0
    swell_h   = (hour.get("swell_height_m") or 0) * spot.exposure
    swell_p   = hour.get("swell_period_s") or 0
    wind_dir  = hour.get("wind_direction_deg") or 0
    wind_spd  = hour.get("wind_speed_mph") or 0

    in_shadow = any(direction_in_window(swell_dir, smin, smax) for smin, smax in spot.shadow_zones)
    near_window = direction_in_window(swell_dir, spot.swell_dir_min - 25, spot.swell_dir_max + 25)
    is_wrap = in_shadow and near_window and spot.wrap_factor > 0 and swell_p >= 14

    if in_shadow and not is_wrap:
        s_dir, swell_h = 0.0, swell_h * 0.05
    elif is_wrap:
        s_dir, swell_h = spot.wrap_factor * 0.7, swell_h * spot.wrap_factor * 0.6
    else:
        s_dir = direction_score(swell_dir, spot.swell_dir_ideal, spot.swell_dir_min, spot.swell_dir_max)

    s_period = score_period(swell_p, spot.swell_period_min, spot.swell_period_ideal)
    s_height = score_height(swell_h, spot.swell_height_min, spot.swell_height_ideal, spot.swell_height_max)
    s_wind   = score_wind(wind_dir, wind_spd, spot)

    weights = {
        "beach": {"dir": 0.30, "period": 0.25, "height": 0.25, "wind": 0.20},
        "reef":  {"dir": 0.25, "period": 0.35, "height": 0.20, "wind": 0.20},
        "point": {"dir": 0.35, "period": 0.30, "height": 0.20, "wind": 0.15},
        # Sandbar-dependent like a beach, but more period-sensitive. Assumed weights.
        "rivermouth": {"dir": 0.30, "period": 0.30, "height": 0.20, "wind": 0.20},
    }
    w = weights.get(spot.break_type, weights["beach"])
    overall = (s_dir * w["dir"] + s_period * w["period"] + s_height * w["height"] + s_wind * w["wind"])

    # Apply Tide modifier if present
    s_tide = 0.0
    if tide_hour and (spot.tide_level_ideal or spot.tide_movement_ideal):
        s_tide = score_tide(tide_hour["height_m"], tide_hour["status"], spot)
        overall += s_tide
        
    overall = round(max(0.0, min(1.0, overall)), 3) # Keep bounded 0-1

    label_map = [(0.85, "PUMPING"), (0.70, "GOOD"), (0.50, "FAIR"), (0.30, "POOR"), (0.00, "FLAT/BLOWN")]
    quality = next(lbl for thresh, lbl in label_map if overall >= thresh)

    return {
        "spot":               spot.name,
        "shore":              spot.shore,
        "effective_height_m": round(swell_h, 2),
        "is_wrap_event":      is_wrap,
        "in_shadow":          in_shadow and not is_wrap,
        "score_direction":    round(s_dir, 3),
        "score_period":       round(s_period, 3),
        "score_height":       round(s_height, 3),
        "score_wind":         round(s_wind, 3),
        "score_tide_mod":     round(s_tide, 3),
        "score_overall":      overall,
        "quality":            quality,
    }


# ─────────────────────────────────────────────────────────────────
#  ENGINE v2
# ─────────────────────────────────────────────────────────────────
# Changes from v1 (each is a guess until checked against real session ratings):
#  - every swell train counts (primary + secondary), combined by energy
#  - periods are PEAK periods (what surfers and Surfline quote), not model mean periods
#  - height is period-aware: longer period surfs bigger (scaled to a 10s reference,
#    so existing height preferences keep their meaning)
#  - direction falls off smoothly outside the window instead of a cliff
#  - light wind (glassy) is good from any direction; gusty wind is penalised
#  - surf_height_m: breaking-height estimate (Komar & Gaughan 1972) for display
#  - tide preference scored against the local tidal range when known
G = 9.81
GLASSY_MPH = 5.0        # at or below this, wind direction barely matters
GLASSY_BLEND_MPH = 5.0  # blend from glassy to normal scoring over the next 5 mph
GUSTY_DELTA_MPH = 10.0  # gusts this far above the mean wind cost 0.1
REF_PERIOD_S = 12.0    # peak-period reference (was 10 s with mean periods; same calibration)
OBLIQUE_BREAKS = ("beach", "rivermouth")   # straight-ish shores: angled swell spreads out and loses height
SURF_RANGE = (0.7, 1.0)  # surf range as fractions of the breaking height (unvalidated: one beach observation)
DIR_FALLOFF = 2.0       # direction factor reaches 0 at 90/DIR_FALLOFF = 45 deg outside the window
ENGINE_WEIGHTS = {
    "beach": {"dir": 0.30, "period": 0.25, "height": 0.25, "wind": 0.20},
    "reef":  {"dir": 0.25, "period": 0.35, "height": 0.20, "wind": 0.20},
    "point": {"dir": 0.35, "period": 0.30, "height": 0.20, "wind": 0.15},
    "rivermouth": {"dir": 0.30, "period": 0.30, "height": 0.20, "wind": 0.20},
}
LABELS = [(0.85, "PUMPING"), (0.70, "GOOD"), (0.50, "FAIR"), (0.30, "POOR"), (0.00, "FLAT/BLOWN")]


def ang_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def outside_window(d: float, dmin: float, dmax: float) -> float:
    """0 inside the window, else degrees to the nearest edge."""
    if direction_in_window(d, dmin, dmax):
        return 0.0
    return min(ang_diff(d, dmin), ang_diff(d, dmax))


def breaking_height(h0: float, period: float) -> float:
    """Komar & Gaughan (1972): Hb = 0.39 g^0.2 (T H0^2)^0.4, deep-water H0 in m, T in s."""
    if h0 <= 0 or period <= 0:
        return 0.0
    return 0.39 * G ** 0.2 * (period * h0 ** 2) ** 0.4


def swell_component(h: float, p: float, d: float, spot: SpotConfig) -> dict:
    """How much of one swell train reaches the spot, and how good its angle is."""
    in_shadow = any(direction_in_window(d, a, b) for a, b in spot.shadow_zones)
    near = direction_in_window(d, spot.swell_dir_min - 25, spot.swell_dir_max + 25)
    is_wrap = in_shadow and near and spot.wrap_factor > 0 and p >= 14
    if in_shadow and not is_wrap:
        factor, s_dir = 0.05, 0.0
    elif is_wrap:
        factor, s_dir = spot.wrap_factor * 0.6, spot.wrap_factor * 0.7
    else:
        out = outside_window(d, spot.swell_dir_min, spot.swell_dir_max)
        if out == 0:
            # scale each side of the window separately, so both edges score 0.3 and
            # meet the outside fall-off without a step (windows are often asymmetric)
            edge = spot.swell_dir_min if direction_in_window(d, spot.swell_dir_min, spot.swell_dir_ideal) else spot.swell_dir_max
            half = max(ang_diff(spot.swell_dir_ideal, edge), 1)
            factor, s_dir = 1.0, 0.3 + 0.7 * max(0.0, 1 - ang_diff(d, spot.swell_dir_ideal) / half)
        else:
            factor = math.cos(math.radians(min(90.0, out * DIR_FALLOFF))) ** 2
            s_dir = 0.3 * factor
    h_arr = h * spot.exposure * factor                       # what arrives, deep-water terms
    if spot.break_type in OBLIQUE_BREAKS:
        # refraction: a swell arriving at angle θ to the beach normal (≈ swell_dir_ideal) spreads its
        # energy along the shore, so height scales ~ sqrt(cos θ) (linear refraction, energy-flux balance)
        theta = min(ang_diff(d, spot.swell_dir_ideal), 85.0)
        h_arr *= math.sqrt(math.cos(math.radians(theta)))
    h_eq = h_arr * (max(p, 1.0) / REF_PERIOD_S) ** 0.4        # period-aware equivalent height
    return {"h_eq": h_eq, "hb": breaking_height(h_arr, p) * spot.height_scale, "s_dir": s_dir,
            "s_period": score_period(p, spot.swell_period_min, spot.swell_period_ideal),
            "in_shadow": in_shadow and not is_wrap, "is_wrap": is_wrap, "dir": d, "period": p}


def score_wind_v2(wdir: Optional[float], wspd: Optional[float], gust: Optional[float], spot: SpotConfig) -> float:
    if wspd is None or wdir is None:
        return 0.5
    if wspd > spot.wind_speed_max:
        s = 0.1
    else:
        k = wspd / max(spot.wind_speed_max, 1)
        out = outside_window(wdir, spot.wind_offshore_min, spot.wind_offshore_max)
        if out == 0:
            normal = max(0.55, 1.0 - 0.45 * k)                # offshore
        elif out <= 45:
            normal = 0.65 - 0.4 * k                           # cross-shore
        else:
            normal = max(0.0, 0.45 - wspd / 25)               # onshore
        if wspd <= GLASSY_MPH:
            s = 0.95
        elif wspd <= GLASSY_MPH + GLASSY_BLEND_MPH:
            t = (wspd - GLASSY_MPH) / GLASSY_BLEND_MPH
            s = 0.95 + (normal - 0.95) * t
        else:
            s = normal
    if gust is not None and wspd > GLASSY_MPH and gust - wspd >= GUSTY_DELTA_MPH:
        s -= 0.1
    return round(max(0.0, min(1.0, s)), 3)


def infer_spot_v2(hour: dict, spot: SpotConfig, tide_hour: dict = None) -> dict:
    trains = []
    for hk, pk, mk, dk in (("swell_height_m", "swell_peak_period_s", "swell_period_s", "swell_direction_deg"),
                           ("swell2_height_m", "swell2_peak_period_s", "swell2_period_s", "swell2_direction_deg")):
        h, d = hour.get(hk), hour.get(dk)
        p = hour.get(pk) or hour.get(mk)                       # peak period; mean if that's all there is
        if h and h > 0.05 and p and d is not None:
            trains.append(swell_component(h, p, d, spot))
    if not trains:
        trains = [swell_component(0.0, hour.get("swell_peak_period_s") or hour.get("swell_period_s") or 1,
                                  hour.get("swell_direction_deg") or 0, spot)]

    energy = [t["h_eq"] ** 2 for t in trains]
    total = sum(energy)
    wts = [e / total for e in energy] if total > 0 else [1 / len(trains)] * len(trains)
    dom = trains[max(range(len(trains)), key=lambda i: energy[i])]
    h_eq = math.sqrt(total)
    hb = math.sqrt(sum(t["hb"] ** 2 for t in trains))

    s_dir    = sum(w * t["s_dir"] for w, t in zip(wts, trains))
    s_period = sum(w * t["s_period"] for w, t in zip(wts, trains))
    s_height = score_height(h_eq, spot.swell_height_min, spot.swell_height_ideal, spot.swell_height_max)
    s_wind   = score_wind_v2(hour.get("wind_direction_deg"), hour.get("wind_speed_mph"), hour.get("wind_gusts_mph"), spot)

    w = ENGINE_WEIGHTS.get(spot.break_type, ENGINE_WEIGHTS["beach"])
    overall = s_dir * w["dir"] + s_period * w["period"] + s_height * w["height"] + s_wind * w["wind"]

    s_tide = 0.0
    if tide_hour and (spot.tide_level_ideal or spot.tide_movement_ideal):
        s_tide = score_tide(tide_hour["height_m"], tide_hour["status"], spot, tide_hour.get("level"))
        overall += s_tide
    overall = round(max(0.0, min(1.0, overall)), 3)

    return {
        "spot": spot.name, "shore": spot.shore,
        "effective_height_m": round(h_eq, 2),
        "surf_height_m":      round(hb, 2),
        "surf_min_m":         round(hb * SURF_RANGE[0], 2),
        "surf_max_m":         round(hb * SURF_RANGE[1], 2),
        "is_wrap_event":      dom["is_wrap"],
        "in_shadow":          dom["in_shadow"],
        "dominant_swell_dir": dom["dir"],
        "dominant_swell_period": dom["period"],
        "score_direction":    round(s_dir, 3),
        "score_period":       round(s_period, 3),
        "score_height":       round(s_height, 3),
        "score_wind":         round(s_wind, 3),
        "score_tide_mod":     round(s_tide, 3),
        "score_overall":      overall,
        "quality":            next(lbl for t, lbl in LABELS if overall >= t),
    }


ENGINES = {"v1": infer_spot_v1, "v2": infer_spot_v2}
DEFAULT_ENGINE = os.environ.get("ENGINE", "v2")


def infer_spot(hour: dict, spot: SpotConfig, tide_hour: dict = None, engine: Optional[str] = None) -> dict:
    return ENGINES[engine or DEFAULT_ENGINE](hour, spot, tide_hour)


# Lowercase quality scale for app clients. Same 0.30/0.50/0.70/0.85 cut points as
# infer_spot's labels, with in-between steps. Matches the keys the web app uses
# (poor, fair_to_good, epic, ...); the full key set there is unverified.
QUALITY_KEYS = [(0.85, "epic"), (0.70, "good"), (0.60, "fair_to_good"), (0.50, "fair"),
                (0.40, "poor_to_fair"), (0.30, "poor"), (0.00, "flat")]


def quality_key(score: float) -> str:
    return next(k for t, k in QUALITY_KEYS if score >= t)


def deg_to_compass(deg: Optional[float]) -> str:
    if deg is None: return "---"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]
