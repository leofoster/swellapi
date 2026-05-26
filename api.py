from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone, timedelta
from surfdeck import (
    SpotConfig,
    infer_spot,
    build_confidence,
    fetch_open_meteo,
    fetch_mock_tides,
    deg_to_compass,
    safe_mean,
    export_forecast_json,
    ISLANDS,
)

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

    break_type:         str   = Field(default="beach", pattern="^(beach|reef|point)$")
    exposure:           float = 1.0
    shadow_zones:       list[list[float]] = []
    wrap_factor:        float = 0.0
    
    # ── New Optional Tide parameters ─────────────────────────────────
    tide_level_ideal:   Optional[str] = Field(default=None, description="e.g., 'low', 'mid', 'high', 'low-mid', 'mid-high'")
    tide_movement_ideal:Optional[str] = Field(default=None, pattern="^(rising|falling)$")

    days:               int   = 3
    timezone:           str   = "UTC"

class HourlyReading(BaseModel):
    time:                str
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
    
    # ── New Tide outputs ─────────────────────────────────────────────
    tide_height_m:       Optional[float]
    tide_status:         Optional[str]
    score_tide_mod:      Optional[float]

    score_direction:     float
    score_period:        float
    score_height:        float
    score_wind:          float
    score_overall:       float
    quality:             str

    effective_height_m:  float
    in_shadow:           bool
    is_wrap:             bool
    confidence:          float
    confidence_label:    str

class DaySummary(BaseModel):
    date:           str
    day_label:      str
    
    # ── New Tide extremes output ─────────────────────────────────────
    high_tides:     list[str]
    low_tides:      list[str]
    
    modal_quality:  str
    avg_score:      float
    avg_height_m:   float
    peak_hour:      str
    peak_quality:   str
    peak_score:     float
    hours:          list[HourlyReading]

class ForecastResponse(BaseModel):
    spot_name:   str
    lat:         float
    lon:         float
    generated:   str
    days:        list[DaySummary]

def request_to_spot_config(req: SpotRequest) -> SpotConfig:
    return SpotConfig(
        name               = req.spot_name,
        shore              = "unknown",
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
        tide_level_ideal   = req.tide_level_ideal,
        tide_movement_ideal= req.tide_movement_ideal,
    )

def build_hourly_reading(
    hour:           dict,
    spot:           SpotConfig,
    buoy_summary:   Optional[dict],
    now:            datetime,
    tide_data:      Optional[dict] = None,
) -> HourlyReading:
    
    dt = datetime.fromisoformat(hour["time"]).astimezone(timezone.utc)
    hours_from_now = max(0, int((dt - now).total_seconds() / 3600))
    
    dt_str_key = dt.strftime("%Y-%m-%dT%H")
    tide_hour = tide_data["hourly"].get(dt_str_key) if tide_data else None

    inferred   = infer_spot(hour, spot, tide_hour)
    confidence = build_confidence(hour, buoy_summary, hours_from_now)

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
        
        tide_height_m            = tide_hour["height_m"] if tide_hour else None,
        tide_status              = tide_hour["status"] if tide_hour else None,
        score_tide_mod           = inferred.get("score_tide_mod", 0.0),

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
    tide_data: Optional[dict] = None,
) -> list[DaySummary]:
    from collections import defaultdict, Counter

    by_date: dict[str, list[HourlyReading]] = defaultdict(list)
    for r in readings:
        date_key = r.time[:10]
        by_date[date_key].append(r)

    today = now.date()
    summaries = []

    for d in range(days):
        date       = today + timedelta(days=d)
        date_str   = str(date)
        day_hours  = by_date.get(date_str, [])

        label = "Today" if d == 0 else "Tomorrow" if d == 1 else date.strftime("%A")
        
        # Format the highs/lows for JSON response
        tide_extremes = tide_data.get("extremes", {}).get(date_str, {"high": [], "low": []}) if tide_data else {"high": [], "low": []}
        highs = [f"{t['time']} ({t['height_m']}m)" for t in tide_extremes.get("high", [])]
        lows  = [f"{t['time']} ({t['height_m']}m)" for t in tide_extremes.get("low", [])]

        if not day_hours:
            summaries.append(DaySummary(
                date=date_str, day_label=label, high_tides=highs, low_tides=lows,
                modal_quality="NO DATA", avg_score=0.0, avg_height_m=0.0, 
                peak_hour="--", peak_quality="NO DATA", peak_score=0.0, hours=[],
            ))
            continue

        scores, heights, qualities = [h.score_overall for h in day_hours], [h.effective_height_m for h in day_hours], [h.quality for h in day_hours]
        best, peak_dt = max(day_hours, key=lambda h: h.score_overall), datetime.fromisoformat(max(day_hours, key=lambda h: h.score_overall).time)

        summaries.append(DaySummary(
            date          = date_str,
            day_label     = label,
            high_tides    = highs,
            low_tides     = lows,
            modal_quality = Counter(qualities).most_common(1)[0][0],
            avg_score     = round(safe_mean(scores) or 0, 3),
            avg_height_m  = round(safe_mean(heights) or 0, 2),
            peak_hour     = peak_dt.strftime("%H:%M"),
            peak_quality  = best.quality,
            peak_score    = round(best.score_overall, 3),
            hours         = day_hours,
        ))
    return summaries

app = FastAPI(title="Surf Forecast API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

@app.post("/forecast", response_model=ForecastResponse)
def post_forecast(req: SpotRequest):
    now  = datetime.now(timezone.utc)
    spot = request_to_spot_config(req)

    fetch_days = min(req.days + 1, 7)
    forecast = fetch_open_meteo(req.lat, req.lon, days=fetch_days, timezone=req.timezone)
    if not forecast: raise HTTPException(status_code=502, detail="Could not fetch Open-Meteo data")
    
    # ── Fetch generated tide data ────────────────────────────────
    tide_data = fetch_mock_tides(req.lat, req.lon, fetch_days, now)

    cap = req.days * 24
    readings = [build_hourly_reading(hour, spot, None, now, tide_data) for hour in forecast[:cap]]
    days_out = group_into_days(readings, req.days, now, tide_data)

    return ForecastResponse(spot_name=req.spot_name, lat=req.lat, lon=req.lon, generated=now.isoformat(timespec="seconds"), days=days_out)

@app.get("/forecast/{island_key}", response_model=ForecastResponse)
def get_island_forecast(island_key: str, days: int = 3):
    if island_key not in ISLANDS: raise HTTPException(status_code=404, detail=f"Unknown island. Available: {list(ISLANDS.keys())}")
    island, spots = ISLANDS[island_key]
    first_spot = next(iter(spots.values()))
    req = SpotRequest(
        spot_name=first_spot.name, lat=island.offshore_lat, lon=island.offshore_lon,
        swell_dir_min=first_spot.swell_dir_min, swell_dir_max=first_spot.swell_dir_max, swell_dir_ideal=first_spot.swell_dir_ideal,
        swell_period_min=first_spot.swell_period_min, swell_period_ideal=first_spot.swell_period_ideal,
        swell_height_min=first_spot.swell_height_min, swell_height_ideal=first_spot.swell_height_ideal, swell_height_max=first_spot.swell_height_max,
        wind_offshore_min=first_spot.wind_offshore_min, wind_offshore_max=first_spot.wind_offshore_max, wind_speed_max=first_spot.wind_speed_max,
        break_type=first_spot.break_type, exposure=first_spot.exposure, days=days,
        tide_level_ideal=first_spot.tide_level_ideal, tide_movement_ideal=first_spot.tide_movement_ideal
    )
    return post_forecast(req)

@app.get("/islands")
def list_islands(): return {"islands": {k: {"name": i.name, "spots": list(s.keys()), "lat": i.offshore_lat, "lon": i.offshore_lon} for k, (i, s) in ISLANDS.items()}}

@app.get("/health")
def health(): return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}