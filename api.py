from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import math

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    raise SystemExit("Python 3.9+ required")

import surfdeck
from surfdeck import (
    SpotConfig,
    UpstreamError,
    infer_spot,
    build_confidence,
    fetch_forecast,
    quality_key,
    get_tides,
    deg_to_compass,
    safe_mean,
    ISLANDS,
)

# ─────────────────────────────────────────────────────────────────
#  MODELS
# ─────────────────────────────────────────────────────────────────

class SpotRequest(BaseModel):
    spot_name:          str   = "Unknown Spot"
    lat:                float
    lon:                float

    swell_dir_min:      float
    swell_dir_max:      float
    swell_dir_ideal:    float
    swell_period_min:   float = 8.0
    swell_period_ideal: float = 14.0
    swell_height_min:   float = 0.5
    swell_height_ideal: float = 1.5
    swell_height_max:   float = 3.5

    wind_offshore_min:  float
    wind_offshore_max:  float
    wind_speed_max:     float = 20.0

    break_type:         str   = Field(default="beach", pattern="^(beach|reef|point|rivermouth)$")
    exposure:           float = 1.0
    shadow_zones:       list[list[float]] = []
    wrap_factor:        float = 0.0

    tide_level_ideal:    Optional[str] = Field(default=None, description="low | mid-low (alias low-mid) | mid | mid-high | high")
    tide_movement_ideal: Optional[str] = Field(default=None, pattern="^(rising|falling)$")

    days:               int   = Field(default=3, ge=1, le=7)
    timezone:           str   = Field(default="UTC", description='IANA zone (e.g. "Atlantic/Bermuda") or "auto" to use the spot\'s own zone')


class HourlyReading(BaseModel):
    time:                str        # ISO 8601 in the request's timezone
    hour_label:          str
    hours_from_now:      int

    swell_height_m:      Optional[float]
    swell_period_s:      Optional[float]
    swell_direction_deg: Optional[float]
    swell_direction_compass: str
    wind_speed_mph:      Optional[float]
    wind_direction_deg:  Optional[float]
    wind_direction_compass: str
    wind_gusts_mph:      Optional[float]

    tide_height_m:       Optional[float]
    tide_status:         Optional[str]
    score_tide_mod:      Optional[float]

    score_direction:     float
    score_period:        float
    score_height:        float
    score_wind:          float
    score_overall:       float      # 0-1
    quality:             str        # legacy uppercase label, e.g. POOR, FLAT/BLOWN
    quality_key:         str        # flat | poor | poor_to_fair | fair | fair_to_good | good | epic

    effective_height_m:  float
    in_shadow:           bool
    is_wrap:             bool
    confidence:          float
    confidence_label:    str


class DaySummary(BaseModel):
    date:           str
    day_label:      str
    high_tides:     list[str] = []   # empty when no tide data
    low_tides:      list[str] = []
    modal_quality:  str
    quality_key:    str              # from avg_score
    avg_score:      float
    avg_height_m:   float
    peak_hour:      str
    peak_quality:   str
    peak_quality_key: str
    peak_score:     float
    hours:          list[HourlyReading]


class ForecastResponse(BaseModel):
    spot_name:   str
    lat:         float
    lon:         float
    timezone:    str              # the zone actually used (resolved when the request said "auto")
    generated:   str
    tides_are_mock: bool           # true only when MOCK_TIDES=1; tide fields are null/empty otherwise
    days:        list[DaySummary]


# ─────────────────────────────────────────────────────────────────
#  CORE
# ─────────────────────────────────────────────────────────────────

def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail=f"Unknown timezone {name!r}")


def request_to_spot_config(req: SpotRequest) -> SpotConfig:
    return SpotConfig(
        name=req.spot_name, shore="unknown",
        swell_dir_min=req.swell_dir_min, swell_dir_max=req.swell_dir_max, swell_dir_ideal=req.swell_dir_ideal,
        swell_period_min=req.swell_period_min, swell_period_ideal=req.swell_period_ideal,
        swell_height_min=req.swell_height_min, swell_height_ideal=req.swell_height_ideal,
        swell_height_max=req.swell_height_max,
        wind_offshore_min=req.wind_offshore_min, wind_offshore_max=req.wind_offshore_max,
        wind_speed_max=req.wind_speed_max,
        break_type=req.break_type, exposure=req.exposure,
        shadow_zones=[tuple(z) for z in req.shadow_zones], wrap_factor=req.wrap_factor,
        tide_level_ideal=req.tide_level_ideal, tide_movement_ideal=req.tide_movement_ideal,
    )


def spot_to_request(spot: SpotConfig, lat: float, lon: float, days: int, tz: str) -> SpotRequest:
    return SpotRequest(
        spot_name=spot.name, lat=lat, lon=lon,
        swell_dir_min=spot.swell_dir_min, swell_dir_max=spot.swell_dir_max, swell_dir_ideal=spot.swell_dir_ideal,
        swell_period_min=spot.swell_period_min, swell_period_ideal=spot.swell_period_ideal,
        swell_height_min=spot.swell_height_min, swell_height_ideal=spot.swell_height_ideal,
        swell_height_max=spot.swell_height_max,
        wind_offshore_min=spot.wind_offshore_min, wind_offshore_max=spot.wind_offshore_max,
        wind_speed_max=spot.wind_speed_max,
        break_type=spot.break_type, exposure=spot.exposure,
        shadow_zones=[list(z) for z in spot.shadow_zones], wrap_factor=spot.wrap_factor,
        tide_level_ideal=spot.tide_level_ideal, tide_movement_ideal=spot.tide_movement_ideal,
        days=days, timezone=tz,
    )


def build_hourly_reading(hour: dict, spot: SpotConfig, buoy_summary: Optional[dict],
                         now: datetime, tz: ZoneInfo, tide_data: Optional[dict] = None) -> HourlyReading:
    dt_utc = datetime.fromisoformat(hour["time"]).astimezone(timezone.utc)
    local  = dt_utc.astimezone(tz)
    hours_from_now = max(0, int((dt_utc - now).total_seconds() // 3600))

    tide_hour  = tide_data["hourly"].get(dt_utc.strftime("%Y-%m-%dT%H")) if tide_data else None
    inferred   = infer_spot(hour, spot, tide_hour)
    confidence = build_confidence(hour, buoy_summary, hours_from_now)

    return HourlyReading(
        time=local.isoformat(), hour_label=local.strftime("%a %H:%M"), hours_from_now=hours_from_now,
        swell_height_m=hour.get("swell_height_m"),
        swell_period_s=hour.get("swell_period_s"),
        swell_direction_deg=hour.get("swell_direction_deg"),
        swell_direction_compass=deg_to_compass(hour.get("swell_direction_deg")),
        wind_speed_mph=hour.get("wind_speed_mph"),
        wind_direction_deg=hour.get("wind_direction_deg"),
        wind_direction_compass=deg_to_compass(hour.get("wind_direction_deg")),
        wind_gusts_mph=hour.get("wind_gusts_mph"),
        tide_height_m=tide_hour["height_m"] if tide_hour else None,
        tide_status=tide_hour["status"] if tide_hour else None,
        score_tide_mod=inferred.get("score_tide_mod", 0.0),
        score_direction=inferred["score_direction"],
        score_period=inferred["score_period"],
        score_height=inferred["score_height"],
        score_wind=inferred["score_wind"],
        score_overall=inferred["score_overall"],
        quality=inferred["quality"],
        quality_key=quality_key(inferred["score_overall"]),
        effective_height_m=inferred["effective_height_m"],
        in_shadow=inferred["in_shadow"],
        is_wrap=inferred["is_wrap_event"],
        confidence=confidence["confidence"],
        confidence_label=confidence["label"],
    )


def group_into_days(readings: list[HourlyReading], days: int, now: datetime, tz: ZoneInfo,
                    tide_data: Optional[dict] = None) -> list[DaySummary]:
    by_date: dict[str, list[HourlyReading]] = defaultdict(list)
    for r in readings:
        by_date[r.time[:10]].append(r)          # r.time is local, so this is the local date

    today = now.astimezone(tz).date()
    summaries = []
    for d in range(days):
        date      = today + timedelta(days=d)
        date_str  = str(date)
        day_hours = by_date.get(date_str, [])
        label     = "Today" if d == 0 else "Tomorrow" if d == 1 else date.strftime("%A")

        ext   = (tide_data or {}).get("extremes", {}).get(date_str, {})
        highs = [f"{t['time']} ({t['height_m']}m)" for t in ext.get("high", [])]
        lows  = [f"{t['time']} ({t['height_m']}m)" for t in ext.get("low", [])]

        if not day_hours:
            summaries.append(DaySummary(
                date=date_str, day_label=label, high_tides=highs, low_tides=lows,
                modal_quality="NO DATA", quality_key="flat", avg_score=0.0, avg_height_m=0.0,
                peak_hour="--", peak_quality="NO DATA", peak_quality_key="flat", peak_score=0.0, hours=[],
            ))
            continue

        best = max(day_hours, key=lambda h: h.score_overall)
        avg  = safe_mean([h.score_overall for h in day_hours]) or 0
        summaries.append(DaySummary(
            date=date_str, day_label=label, high_tides=highs, low_tides=lows,
            modal_quality=Counter(h.quality for h in day_hours).most_common(1)[0][0],
            quality_key=quality_key(avg),
            avg_score=round(avg, 3),
            avg_height_m=round(safe_mean([h.effective_height_m for h in day_hours]) or 0, 2),
            peak_hour=datetime.fromisoformat(best.time).strftime("%H:%M"),
            peak_quality=best.quality,
            peak_quality_key=best.quality_key,
            peak_score=round(best.score_overall, 3),
            hours=day_hours,
        ))
    return summaries


def compute_forecast(req: SpotRequest, now: Optional[datetime] = None) -> ForecastResponse:
    now  = now or datetime.now(timezone.utc)
    spot = request_to_spot_config(req)

    try:
        forecast, spot_tz = fetch_forecast(req.lat, req.lon)
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=f"Open-Meteo: {e}")

    tz_name = spot_tz if req.timezone.lower() == "auto" else req.timezone
    tz = _zone(tz_name)

    # Tides are optional: any failure means "no tide data", never a failed forecast.
    try:
        tide_data = get_tides(req.lat, req.lon, req.days + 1, now)
    except Exception:
        tide_data = None

    # Keep hours from local midnight today through the last requested day.
    start = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tz)
    end   = start + timedelta(days=req.days)
    hours = [h for h in forecast if start <= datetime.fromisoformat(h["time"]) < end]

    readings = [build_hourly_reading(h, spot, None, now, tz, tide_data) for h in hours]
    return ForecastResponse(
        spot_name=req.spot_name, lat=req.lat, lon=req.lon, timezone=tz_name,
        generated=now.isoformat(timespec="seconds"),
        tides_are_mock=tide_data is not None,
        days=group_into_days(readings, req.days, now, tz, tide_data),
    )


def _island(island_key: str):
    if island_key not in ISLANDS:
        raise HTTPException(status_code=404, detail=f"Unknown island. Available: {list(ISLANDS)}")
    return ISLANDS[island_key]


def island_forecast(island_key: str, days: int = 3, spot_key: Optional[str] = None,
                    now: Optional[datetime] = None) -> ForecastResponse:
    island, spots = _island(island_key)
    if spot_key is None:
        spot_key = next(iter(spots))
    if spot_key not in spots:
        raise HTTPException(status_code=404, detail=f"Unknown spot. Available: {list(spots)}")
    req = spot_to_request(spots[spot_key], island.offshore_lat, island.offshore_lon, days, island.timezone)
    return compute_forecast(req, now)


# ─────────────────────────────────────────────────────────────────
#  ESP32 PAYLOAD — 3-hour blocks, short keys, ~1 KB
# ─────────────────────────────────────────────────────────────────

# Mirrors the thresholds in surfdeck.infer_spot(); "FLAT" fits the screen better than "FLAT/BLOWN".
ESP_LABELS = [(0.85, "PUMPING"), (0.70, "GOOD"), (0.50, "FAIR"), (0.30, "POOR"), (0.00, "FLAT")]


def _label_for(score: float) -> str:
    return next(lbl for thresh, lbl in ESP_LABELS if score >= thresh)


def _circular_mean(degrees: list) -> Optional[float]:
    """Mean of compass bearings. A plain average of 350 and 10 gives 180, not 0."""
    vals = [d for d in degrees if d is not None]
    if not vals:
        return None
    x = safe_mean([math.cos(math.radians(d)) for d in vals])
    y = safe_mean([math.sin(math.radians(d)) for d in vals])
    return round(math.degrees(math.atan2(y, x)) % 360, 1)


class EspBlock(BaseModel):
    t:  str              # local label, e.g. "Mon 15"
    e:  int              # block start, unix seconds. Devices compute "+Nh" from this.
    hn: int              # hours from generation time to block start (legacy; prefer e)
    h:  Optional[float]  # effective swell height m
    p:  Optional[float]  # swell period s
    sd: str              # swell direction compass
    sg: Optional[float]  # swell direction deg
    w:  Optional[float]  # wind mph
    wd: str              # wind direction compass
    wg: Optional[float]  # wind direction deg
    r:  float            # rating 0-1
    q:  str              # rating label


class EspResponse(BaseModel):
    v:  int = 2          # payload version
    s:  str              # spot name
    g:  str              # generated, UTC ISO
    gt: int              # generated, unix seconds
    b:  list[EspBlock]


def esp_from_forecast(full: ForecastResponse, blocks: int, now: datetime) -> EspResponse:
    tz = _zone(full.timezone)
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    readings = sorted(
        (h for d in full.days for h in d.hours if datetime.fromisoformat(h.time) >= this_hour),
        key=lambda h: datetime.fromisoformat(h.time),
    )
    if not readings:
        raise HTTPException(status_code=502, detail="No hourly data available")

    out: list[EspBlock] = []
    for i in range(0, len(readings), 3):
        if len(out) >= blocks:
            break
        chunk = readings[i:i + 3]
        start = datetime.fromisoformat(chunk[0].time)
        score = round(safe_mean([h.score_overall for h in chunk]) or 0.0, 3)
        sdeg  = _circular_mean([h.swell_direction_deg for h in chunk])
        wdeg  = _circular_mean([h.wind_direction_deg for h in chunk])
        height = safe_mean([h.effective_height_m for h in chunk])
        period = safe_mean([h.swell_period_s for h in chunk])
        wind   = safe_mean([h.wind_speed_mph for h in chunk])
        out.append(EspBlock(
            t=start.astimezone(tz).strftime("%a %H"),
            e=int(start.timestamp()),
            hn=max(0, int((start - now).total_seconds() // 3600)),
            h=round(height, 2) if height is not None else None,
            p=round(period, 1) if period is not None else None,
            sd=deg_to_compass(sdeg), sg=sdeg,
            w=round(wind, 1) if wind is not None else None,
            wd=deg_to_compass(wdeg), wg=wdeg,
            r=score, q=_label_for(score),
        ))
    return EspResponse(s=full.spot_name, g=now.isoformat(timespec="seconds"),
                       gt=int(now.timestamp()), b=out)


def _esp_days(blocks: int) -> int:
    return min(-(-(blocks * 3) // 24) + 1, 7)


def esp_forecast(island_key: str, blocks: int = 8, spot_key: Optional[str] = None,
                 now: Optional[datetime] = None) -> EspResponse:
    now = now or datetime.now(timezone.utc)
    full = island_forecast(island_key, days=_esp_days(blocks), spot_key=spot_key, now=now)
    return esp_from_forecast(full, blocks, now)


def esp_for_spot(req: SpotRequest, blocks: int = 8, now: Optional[datetime] = None) -> EspResponse:
    """Device payload for any spot profile, e.g. one saved in the web app."""
    now = now or datetime.now(timezone.utc)
    req = req.model_copy(update={"days": _esp_days(blocks)})
    return esp_from_forecast(compute_forecast(req, now), blocks, now)


# ─────────────────────────────────────────────────────────────────
#  HTTP
# ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Surf Forecast API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

_SIM_DIR = Path(__file__).parent / "simulator"
if _SIM_DIR.is_dir():
    app.mount("/sim", StaticFiles(directory=_SIM_DIR, html=True), name="sim")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/sim/" if _SIM_DIR.is_dir() else "/docs")


@app.post("/forecast", response_model=ForecastResponse)
def post_forecast(req: SpotRequest):
    return compute_forecast(req)


@app.get("/forecast/{island_key}", response_model=ForecastResponse)
def get_island_forecast(island_key: str, days: int = Query(3, ge=1, le=7), spot: Optional[str] = None):
    return island_forecast(island_key, days=days, spot_key=spot)


@app.get("/esp/{island_key}", response_model=EspResponse)
def get_esp_forecast(island_key: str, blocks: int = Query(8, ge=1, le=16), spot: Optional[str] = None):
    """Compact 3-hour-averaged forecast sized for a microcontroller display."""
    return esp_forecast(island_key, blocks=blocks, spot_key=spot)


@app.get("/map/{island_key}")
def get_swell_map(island_key: str):
    """Swell height/direction/period grid around the island for the next 12h."""
    import swellmap
    if island_key not in swellmap.MAP_REGIONS:
        raise HTTPException(status_code=404, detail=f"No map region. Available: {list(swellmap.MAP_REGIONS)}")
    if island_key == "global":
        raise HTTPException(status_code=404, detail="Global map is published statically only (map/global.json); "
                                                    "it costs ~470 Open-Meteo calls to build.")
    try:
        return swellmap.fetch_swell_grid(island_key)
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=f"Open-Meteo: {e}")


@app.post("/esp", response_model=EspResponse)
def post_esp_forecast(req: SpotRequest, blocks: int = Query(8, ge=1, le=16)):
    """Same compact device payload as /esp/{island}, for any spot profile (same body as POST /forecast)."""
    return esp_for_spot(req, blocks=blocks)


@app.get("/islands")
def list_islands():
    return {"islands": {k: {"name": i.name, "timezone": i.timezone, "spots": list(s),
                            "lat": i.offshore_lat, "lon": i.offshore_lon}
                        for k, (i, s) in ISLANDS.items()}}


@app.get("/debug/upstream")
def debug_upstream(lat: float = 32.30, lon: float = -64.80):
    """What Open-Meteo says from this server, over the same route the app uses
    (direct, or via OPEN_METEO_PROXY when set)."""
    out = {"route": surfdeck.OPEN_METEO_PROXY or "direct"}
    for name, url, extra in [("marine", surfdeck.MARINE_URL, {"hourly": "swell_wave_height"}),
                             ("weather", surfdeck.WEATHER_URL, {"hourly": "wind_speed_10m"})]:
        try:
            r = surfdeck._session.get(url, params={"latitude": lat, "longitude": lon, "forecast_days": 1, **extra},
                                      headers=surfdeck._PROXY_HEADERS, timeout=10)
            try:
                body = r.json()
            except ValueError:
                body = {}
            out[name] = {"status": r.status_code, "reason": body.get("reason"),
                         "has_hourly": "hourly" in body, "snippet": r.text[:200]}
        except Exception as e:
            out[name] = {"status": None, "exception": f"{type(e).__name__}: {e}"}
    return out


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
