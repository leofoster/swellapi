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
import requests
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
import statistics
import json
from collections import Counter


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
    timezone: str = "auto"

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
            timezone="America/Halifax",
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

def fetch_open_meteo(lat: float, lon: float, days: int = 7, timezone: str = "auto") -> Optional[list]:
    try:
        marine = requests.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude": lat, "longitude": lon, "timezone": timezone, "forecast_days": days,
                "hourly": [
                    "wave_height", "wave_direction", "wave_period", "swell_wave_height", 
                    "swell_wave_direction", "swell_wave_period", "swell_wave_peak_period",
                    "wind_wave_height", "wind_wave_period",
                ],
            }, timeout=10
        ).json()

        weather = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon, "timezone": timezone, "forecast_days": days, 
                "wind_speed_unit": "mph",
                "hourly": ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"],
            }, timeout=10
        ).json()
    except Exception as e:
        print(f"  ❌ Open-Meteo error: {e}")
        return None

    mh, wh = marine["hourly"], weather["hourly"]
    return [{
        "time":                mh["time"][i],
        "swell_height_m":      mh["swell_wave_height"][i],
        "swell_direction_deg": mh["swell_wave_direction"][i],
        "swell_period_s":      mh["swell_wave_period"][i],
        "swell_peak_period_s": mh["swell_wave_peak_period"][i],
        "wave_height_m":       mh["wave_height"][i],
        "wind_wave_height_m":  mh["wind_wave_height"][i],
        "wind_wave_period_s":  mh["wind_wave_period"][i],
        "wind_speed_mph":      wh["wind_speed_10m"][i],
        "wind_direction_deg":  wh["wind_direction_10m"][i],
        "wind_gusts_mph":      wh["wind_gusts_10m"][i],
    } for i in range(len(mh["time"]))]


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


def fetch_ndbc_buoy(buoy_id: str) -> Optional[dict]:
    url = f"https://www.ndbc.noaa.gov/data/realtime2/{buoy_id}.txt"
    try:
        resp = requests.get(url, timeout=10)
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
        print(f"  ⚠️  Buoy {buoy_id} unavailable: {e}")
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

def score_tide(tide_height: float, tide_status: str, spot: SpotConfig) -> float:
    """Calculates a modifier between -0.15 and +0.15 based on tide preferences."""
    modifier = 0.0
    # Assuming standard 0.5 to 3.5m range for normalization 
    norm = min(max((tide_height - 0.5) / 3.0, 0.0), 1.0) 
    
    level_map = {"low": 0.0, "low-mid": 0.25, "mid": 0.5, "mid-high": 0.75, "high": 1.0}
    
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

def infer_spot(hour: dict, spot: SpotConfig, tide_hour: dict = None) -> dict:
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


def deg_to_compass(deg: Optional[float]) -> str:
    if deg is None: return "---"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]

def _condense_day_hours(hours: list[dict], spot: SpotConfig, buoy_summary: Optional[dict], now: datetime, tide_data: Optional[dict] = None) -> dict:
    if not hours: return {"quality": "NO DATA", "score_overall": 0.0}

    inferred_list, confidence_list, quality_labels = [], [], []

    for hour in hours:
        dt = datetime.fromisoformat(hour["time"]).astimezone(timezone.utc)
        hours_from_now = max(0, int((dt - now).total_seconds() / 3600))
        
        tide_hour = tide_data["hourly"].get(dt.strftime("%Y-%m-%dT%H")) if tide_data else None
        
        inf = infer_spot(hour, spot, tide_hour)
        conf = build_confidence(hour, buoy_summary, hours_from_now)
        inferred_list.append(inf)
        confidence_list.append(conf)
        quality_labels.append(inf["quality"])

    avg_score   = safe_mean([i["score_overall"] for i in inferred_list])
    avg_height  = safe_mean([i["effective_height_m"] for i in inferred_list])
    avg_conf    = safe_mean([c["confidence"] for c in confidence_list])
    modal_quality = Counter(quality_labels).most_common(1)[0][0]
    avg_conf_label = "HIGH" if (avg_conf or 0) >= 0.75 else "MEDIUM" if (avg_conf or 0) >= 0.50 else "LOW"

    best_idx  = max(range(len(inferred_list)), key=lambda i: inferred_list[i]["score_overall"])
    best_inf, best_conf, best_hour = inferred_list[best_idx], confidence_list[best_idx], hours[best_idx]
    peak_dt = datetime.fromisoformat(best_hour["time"]).astimezone(timezone.utc)

    all_flags = []
    for c in confidence_list: all_flags.extend(c["flags"])
    unique_flags = list(dict.fromkeys(f for f in all_flags if "confirms model" not in f))[:3]

    return {
        "quality":           modal_quality,
        "score_overall":     round(avg_score or 0, 3),
        "avg_height_m":      round(avg_height or 0, 2),
        "confidence":        round(avg_conf or 0, 2),
        "confidence_label":  avg_conf_label,
        "spot_name":         spot.name,
        "shore":             spot.shore,
        "break_type":        spot.break_type,
        "peak": {
            "time":          peak_dt.strftime("%H:%M"),
            "score":         round(best_inf["score_overall"], 3),
            "quality":       best_inf["quality"],
            "swell_height_m":best_inf["effective_height_m"],
            "swell_period_s":best_hour.get("swell_period_s"),
            "swell_dir":     deg_to_compass(best_hour.get("swell_direction_deg")),
            "wind_speed_mph":best_hour.get("wind_speed_mph"),
            "wind_dir":      deg_to_compass(best_hour.get("wind_direction_deg")),
            "is_wrap":       best_inf["is_wrap_event"],
            "in_shadow":     best_inf["in_shadow"],
            "confidence":    round(best_conf["confidence"], 2),
        },
        "flags": unique_flags,
    }

def export_forecast_json(island_key: str, days_ahead: int = 3, output_path: Optional[str] = None) -> dict:
    island, spots = ISLANDS[island_key]
    now = datetime.now(timezone.utc)
    today = now.date()

    buoys = fetch_all_buoys(island.buoy_ids)
    buoy_summary = summarise_buoys(buoys)

    fetch_days = days_ahead + 2
    forecast = fetch_open_meteo(island.offshore_lat, island.offshore_lon, days=fetch_days, timezone=island.timezone)
    if not forecast: raise RuntimeError("Could not fetch model data from Open-Meteo.")
    
    # Generate tides for the same span
    tide_data = fetch_mock_tides(island.offshore_lat, island.offshore_lon, fetch_days, now)

    by_date: dict[str, list[dict]] = {}
    for hour in forecast:
        date_key = hour["time"][:10]
        by_date.setdefault(date_key, []).append(hour)

    target_dates = [str(today + timedelta(days=d)) for d in range(days_ahead + 1)]

    days_out = []
    for idx, date_str in enumerate(target_dates):
        label = "Today" if idx == 0 else "Tomorrow" if idx == 1 else datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        day_hours = by_date.get(date_str, [])
        spot_readings = {}
        for spot_key, spot in spots.items():
            spot_readings[spot_key] = _condense_day_hours(day_hours, spot, buoy_summary, now, tide_data)

        # Grab daily tide extremes
        tide_extremes = tide_data.get("extremes", {}).get(date_str, {"high": [], "low": []})

        days_out.append({
            "date":  date_str,
            "label": label,
            "high_tides": [f"{t['time']} ({t['height_m']}m)" for t in tide_extremes.get("high", [])],
            "low_tides":  [f"{t['time']} ({t['height_m']}m)" for t in tide_extremes.get("low", [])],
            "spots": spot_readings,
        })

    payload = {
        "island":      island.name,
        "island_key":  island_key,
        "generated":   now.isoformat(timespec="seconds"),
        "buoy":        buoy_summary,
        "days":        days_out,
    }

    if output_path:
        with open(output_path, "w") as f: json.dump(payload, f, indent=2, default=str)
    return payload