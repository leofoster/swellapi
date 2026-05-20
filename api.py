from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from surfdeck import (
    SpotConfig,
    infer_spot,
    build_confidence,
    fetch_open_meteo,
    deg_to_compass,
    safe_mean,
    export_forecast_json,
    ISLANDS,
)

app = FastAPI(title="Surf Forecast API")

# This lets your website call it from a browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # lock this down to your domain in production
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/forecast/{island_key}")
def get_forecast(
    island_key: str,
    days: int = Query(default=3, ge=1, le=7),
):
    try:
        data = export_forecast_json(island_key=island_key, days_ahead=days)
        return data
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown island: {island_key}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────────────
#  REQUEST MODEL
#  Pydantic validates every field automatically — bad requests
#  get a clean 422 error with exactly which field is wrong.
# ─────────────────────────────────────────────────────────────────


class SpotRequest(BaseModel):
    # Location
    spot_name:          str   = "Unknown Spot"
    lat:                float
    lon:                float

    # Swell direction window (degrees)
    swell_dir_min:      float
    swell_dir_max:      float
    swell_dir_ideal:    float

    # Period thresholds (seconds)
    swell_period_min:   float = 8.0
    swell_period_ideal: float = 14.0

    # Height thresholds (metres)
    swell_height_min:   float = 0.5
    swell_height_ideal: float = 1.5
    swell_height_max:   float = 3.5

    # Wind
    wind_offshore_min:  float
    wind_offshore_max:  float
    wind_speed_max:     float = 20.0

    # Break character
    break_type:         str   = Field(default="beach",
                                      pattern="^(beach|reef|point)$")
    exposure:           float = 1.0

    # Optional — wrap and shadow zones
    # Send as list of [min_deg, max_deg] pairs
    shadow_zones:       list[list[float]] = []
    wrap_factor:        float = 0.0

    # Forecast options
    days:               int   = 3      # 1–7
    timezone:           str   = "UTC"


# ─────────────────────────────────────────────────────────────────
#  RESPONSE MODEL
#  Typed output — makes it easy to consume on ESP32 or frontend.
# ─────────────────────────────────────────────────────────────────

class HourlyReading(BaseModel):
    time:                str
    hour_label:          str            # "Mon 08:00"
    hours_from_now:      int

    # Raw offshore data
    swell_height_m:      Optional[float]
    swell_period_s:      Optional[float]
    swell_direction_deg: Optional[float]
    swell_direction_compass: str
    wind_speed_mph:      Optional[float]
    wind_direction_deg:  Optional[float]
    wind_direction_compass: str
    wind_gusts_mph:      Optional[float]

    # Scoring
    score_direction:     float
    score_period:        float
    score_height:        float
    score_wind:          float
    score_overall:       float
    quality:             str            # PUMPING / GOOD / FAIR / POOR / FLAT/BLOWN

    # Effective height after exposure + shadow
    effective_height_m:  float
    in_shadow:           bool
    is_wrap:             bool

    # Confidence
    confidence:          float
    confidence_label:    str


class DaySummary(BaseModel):
    date:           str
    day_label:      str              # "Today" / "Tomorrow" / "Wednesday"
    modal_quality:  str
    avg_score:      float
    avg_height_m:   float
    peak_hour:      str              # time of best hour e.g. "14:00"
    peak_quality:   str
    peak_score:     float
    hours:          list[HourlyReading]


class ForecastResponse(BaseModel):
    spot_name:   str
    lat:         float
    lon:         float
    generated:   str
    days:        list[DaySummary]


# ─────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────

def request_to_spot_config(req: SpotRequest) -> SpotConfig:
    """Convert the Pydantic request model into your existing SpotConfig."""
    return SpotConfig(
        name               = req.spot_name,
        shore              = "unknown",       # not needed for scoring
        swell_dir_min      = req.swell_dir_min,
        swell_dir_max      = req.swell_dir_max,
        swell_dir_ideal    = req.swell_dir_ideal,
        swell_period_min   = req.swell_period_min,
        swell_period_ideal = req.swell_period_ideal,
        swell_height_min   = req.swell_height_min,
        swell_height_ideal = req.swell_height_ideal,
        swell_height_max   = req.swell_height_max,
        wind_offshore_min  = req.wind_offshore_min,
        wind_offshore_max  = req.wind_offshore_max,
        wind_speed_max     = req.wind_speed_max,
        break_type         = req.break_type,
        exposure           = req.exposure,
        shadow_zones       = [tuple(z) for z in req.shadow_zones],
        wrap_factor        = req.wrap_factor,
    )


def build_hourly_reading(
    hour:           dict,
    spot:           SpotConfig,
    buoy_summary:   Optional[dict],
    now:            datetime,
) -> HourlyReading:
    """Run the full inference + confidence pipeline for one hour."""
    dt             = datetime.fromisoformat(hour["time"]).astimezone(timezone.utc)
    hours_from_now = max(0, int((dt - now).total_seconds() / 3600))
    inferred       = infer_spot(hour, spot)
    confidence     = build_confidence(hour, buoy_summary, hours_from_now)

    return HourlyReading(
        time                     = dt.isoformat(),
        hour_label               = dt.strftime("%a %H:%M"),
        hours_from_now           = hours_from_now,

        swell_height_m           = hour.get("swell_height_m"),
        swell_period_s           = hour.get("swell_period_s"),
        swell_direction_deg      = hour.get("swell_direction_deg"),
        swell_direction_compass  = deg_to_compass(hour.get("swell_direction_deg")),
        wind_speed_mph           = hour.get("wind_speed_mph"),
        wind_direction_deg       = hour.get("wind_direction_deg"),
        wind_direction_compass   = deg_to_compass(hour.get("wind_direction_deg")),
        wind_gusts_mph           = hour.get("wind_gusts_mph"),

        score_direction          = inferred["score_direction"],
        score_period             = inferred["score_period"],
        score_height             = inferred["score_height"],
        score_wind               = inferred["score_wind"],
        score_overall            = inferred["score_overall"],
        quality                  = inferred["quality"],

        effective_height_m       = inferred["effective_height_m"],
        in_shadow                = inferred["in_shadow"],
        is_wrap                  = inferred["is_wrap_event"],

        confidence               = confidence["confidence"],
        confidence_label         = confidence["label"],
    )


def group_into_days(
    readings: list[HourlyReading],
    days:     int,
    now:      datetime,
) -> list[DaySummary]:
    """Bucket hourly readings into calendar days, build summaries."""
    from collections import defaultdict, Counter

    by_date: dict[str, list[HourlyReading]] = defaultdict(list)
    for r in readings:
        date_key = r.time[:10]
        by_date[date_key].append(r)

    today    = now.date()
    summaries = []

    for d in range(days):
        date       = today + timedelta(days=d)
        date_str   = str(date)
        day_hours  = by_date.get(date_str, [])

        if d == 0:      label = "Today"
        elif d == 1:    label = "Tomorrow"
        else:           label = date.strftime("%A")

        if not day_hours:
            summaries.append(DaySummary(
                date=date_str, day_label=label,
                modal_quality="NO DATA", avg_score=0.0,
                avg_height_m=0.0, peak_hour="--",
                peak_quality="NO DATA", peak_score=0.0,
                hours=[],
            ))
            continue

        scores    = [h.score_overall     for h in day_hours]
        heights   = [h.effective_height_m for h in day_hours]
        qualities = [h.quality           for h in day_hours]

        best      = max(day_hours, key=lambda h: h.score_overall)
        peak_dt   = datetime.fromisoformat(best.time)

        summaries.append(DaySummary(
            date          = date_str,
            day_label     = label,
            modal_quality = Counter(qualities).most_common(1)[0][0],
            avg_score     = round(safe_mean(scores) or 0, 3),
            avg_height_m  = round(safe_mean(heights) or 0, 2),
            peak_hour     = peak_dt.strftime("%H:%M"),
            peak_quality  = best.quality,
            peak_score    = round(best.score_overall, 3),
            hours         = day_hours,
        ))

    return summaries


# ─────────────────────────────────────────────────────────────────
#  ENDPOINT
# ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Surf Forecast API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

@app.post("/forecast", response_model=ForecastResponse)
def post_forecast(req: SpotRequest):
    now  = datetime.now(timezone.utc)
    spot = request_to_spot_config(req)

    # ── Fetch model data ─────────────────────────────────────────
    forecast = fetch_open_meteo(
        req.lat, req.lon,
        days     = min(req.days + 1, 7),   # +1 buffer for day boundaries
        timezone = req.timezone,
    )
    if not forecast:
        raise HTTPException(
            status_code=502,
            detail="Could not fetch forecast data from Open-Meteo"
        )

    # ── No buoy for arbitrary locations — model only ─────────────
    # Could extend this later to find nearest NDBC buoy automatically
    buoy_summary = None

    # ── Build hourly readings ────────────────────────────────────
    # Cap at days * 24 hours so we don't return a week of data
    cap      = req.days * 24
    readings = [
        build_hourly_reading(hour, spot, buoy_summary, now)
        for hour in forecast[:cap]
    ]

    # ── Group into days ──────────────────────────────────────────
    days_out = group_into_days(readings, req.days, now)

    return ForecastResponse(
        spot_name = req.spot_name,
        lat       = req.lat,
        lon       = req.lon,
        generated = now.isoformat(timespec="seconds"),
        days      = days_out,
    )
# ── Convenience GET endpoint for pre-configured islands ──────────
@app.get("/forecast/{island_key}", response_model=ForecastResponse)
def get_island_forecast(island_key: str, days: int = 3):
    if island_key not in ISLANDS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown island '{island_key}'. "
                   f"Available: {list(ISLANDS.keys())}"
        )
    island, spots = ISLANDS[island_key]
    # Build a request from the first spot in the island definition
    first_spot = next(iter(spots.values()))
    req = SpotRequest(
        spot_name          = first_spot.name,
        lat                = island.offshore_lat,
        lon                = island.offshore_lon,
        swell_dir_min      = first_spot.swell_dir_min,
        swell_dir_max      = first_spot.swell_dir_max,
        swell_dir_ideal    = first_spot.swell_dir_ideal,
        swell_period_min   = first_spot.swell_period_min,
        swell_period_ideal = first_spot.swell_period_ideal,
        swell_height_min   = first_spot.swell_height_min,
        swell_height_ideal = first_spot.swell_height_ideal,
        swell_height_max   = first_spot.swell_height_max,
        wind_offshore_min  = first_spot.wind_offshore_min,
        wind_offshore_max  = first_spot.wind_offshore_max,
        wind_speed_max     = first_spot.wind_speed_max,
        break_type         = first_spot.break_type,
        exposure           = first_spot.exposure,
        days               = days,
    )
    return post_forecast(req)


@app.get("/islands")
def list_islands():
    return {
        "islands": {
            key: {
                "name":  island.name,
                "spots": list(spots.keys()),
                "lat":   island.offshore_lat,
                "lon":   island.offshore_lon,
            }
            for key, (island, spots) in ISLANDS.items()
        }
    }

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}