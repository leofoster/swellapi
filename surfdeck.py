"""
Island Surf Forecast
─────────────────────────────────────────────────────────────────
Architecture:
  1. Fetch one central offshore position (the island's open ocean reading)
  2. Pull nearest NOAA buoy as ground truth
  3. Cross-reference both — build a confidence score
  4. Infer conditions for each shore from the single dataset
  5. Each SpotConfig is just a directional filter + local modifier

This works well for any island or peninsula where one offshore
reading represents the swell environment for the whole location.
"""

import requests
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
import statistics
import json
from collections import Counter

# ─────────────────────────────────────────────────────────────────
#  SAFE AVERAGING HELPER
#  Handles 0, 1, or many values without touching statistics.mean()
#  directly — which raises StatisticsError on empty sequences.
# ─────────────────────────────────────────────────────────────────

def safe_mean(values: list) -> Optional[float]:
    """
    Return the mean of a list of non-None floats.
    Returns None (never raises) if the list is empty or all-None.
    Works correctly for 0, 1, or many items.
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    # statistics.mean needs >= 1 item — guaranteed here
    return statistics.mean(clean)


# ─────────────────────────────────────────────────────────────────
#  ISLAND DEFINITION
# ─────────────────────────────────────────────────────────────────

@dataclass
class Island:
    name: str
    offshore_lat: float
    offshore_lon: float
    # NOAA NDBC buoy IDs — find yours at https://www.ndbc.noaa.gov/
    # Can be empty list, single item, or many — all handled safely
    buoy_ids: list[str]
    timezone: str = "auto"


# ─────────────────────────────────────────────────────────────────
#  SPOT CONFIGURATION
# ─────────────────────────────────────────────────────────────────

@dataclass
class SpotConfig:
    name: str
    shore: str                      # "north" / "south" / "east" / "west"

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

    break_type: str                 # "beach" / "reef" / "point"

    # 1.0 = fully exposed, <1.0 = sheltered, >1.0 = swell focusing
    exposure: float = 1.0

    # List of (min_deg, max_deg) ranges completely blocked by land
    shadow_zones: list = field(default_factory=list)

    # 0.0 = no wrap, 0.4 = moderate wrap for long-period swell
    wrap_factor: float = 0.0

    notes: str = ""


# ─────────────────────────────────────────────────────────────────
#  ISLAND + SPOT DEFINITIONS
# ─────────────────────────────────────────────────────────────────

ISLANDS: dict[str, tuple[Island, dict[str, SpotConfig]]] = {

    "bermuda": (
        Island(
            name="Bermuda",
            offshore_lat=32.30,
            offshore_lon=-64.80,
            # 41049 is the only reliably active NDBC buoy near Bermuda.
            # Add more IDs here if new buoys come online.
            buoy_ids=["41049"],
            timezone="America/Halifax",
        ),
        {
            "south_shore": SpotConfig(
                name="Southlands",
                shore="south",
                swell_dir_min=135,
                swell_dir_max=260,
                swell_dir_ideal=180,
                swell_period_min=10,
                swell_period_ideal=16,
                swell_height_min=0.8,
                swell_height_ideal=2.0,
                swell_height_max=4.5,
                wind_offshore_min=315,
                wind_offshore_max=60,
                wind_speed_max=20,
                break_type="reef",
                exposure=1.0,
                shadow_zones=[(260, 315)],
                wrap_factor=0.2,
                notes="Best surf in Bermuda. Needs S or SSW groundswell. "
                      "Hurricane swells Aug-Oct are the standout events.",
            ),
            "north_shore": SpotConfig(
                name="North Rock",
                shore="north",
                swell_dir_min=300,
                swell_dir_max=60,
                swell_dir_ideal=350,
                swell_period_min=9,
                swell_period_ideal=14,
                swell_height_min=0.6,
                swell_height_ideal=1.5,
                swell_height_max=3.0,
                wind_offshore_min=135,
                wind_offshore_max=225,
                wind_speed_max=18,
                break_type="reef",
                exposure=0.8,
                shadow_zones=[(60, 300)],
                wrap_factor=0.15,
                notes="Works winter N Atlantic swells. More sheltered than south shore.",
            ),
        }
    ),

    "newquay": (
        Island(
            name="Newquay, Cornwall",
            offshore_lat=50.35,
            offshore_lon=-5.30,
            buoy_ids=["62029", "62105"],
            timezone="Europe/London",
        ),
        {
            "fistral": SpotConfig(
                name="Fistral Beach",
                shore="west",
                swell_dir_min=180,
                swell_dir_max=315,
                swell_dir_ideal=250,
                swell_period_min=8,
                swell_period_ideal=14,
                swell_height_min=0.5,
                swell_height_ideal=1.5,
                swell_height_max=3.5,
                wind_offshore_min=45,
                wind_offshore_max=135,
                wind_speed_max=20,
                break_type="beach",
                exposure=1.0,
                shadow_zones=[(315, 180)],
                notes="Picks up all W and SW Atlantic swell. "
                      "Most consistent beach in Cornwall.",
            ),
            "towan": SpotConfig(
                name="Towan Beach",
                shore="west",
                swell_dir_min=200,
                swell_dir_max=300,
                swell_dir_ideal=250,
                swell_period_min=9,
                swell_period_ideal=13,
                swell_height_min=0.6,
                swell_height_ideal=1.2,
                swell_height_max=2.5,
                wind_offshore_min=60,
                wind_offshore_max=120,
                wind_speed_max=18,
                break_type="beach",
                exposure=0.75,
                shadow_zones=[(300, 200)],
                notes="More sheltered than Fistral. Better on bigger swells.",
            ),
        }
    ),
}


# ─────────────────────────────────────────────────────────────────
#  DATA FETCHING
# ─────────────────────────────────────────────────────────────────

def fetch_open_meteo(lat: float, lon: float, days: int = 7,
                     timezone: str = "auto") -> Optional[list]:
    """Fetch marine + weather data from Open-Meteo."""
    try:
        marine = requests.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude": lat, "longitude": lon, "timezone": timezone,
                "forecast_days": days,
                "hourly": [
                    "wave_height", "wave_direction", "wave_period",
                    "swell_wave_height", "swell_wave_direction",
                    "swell_wave_period", "swell_wave_peak_period",
                    "wind_wave_height", "wind_wave_period",
                ],
            },
            timeout=10
        ).json()

        weather = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon, "timezone": timezone,
                "forecast_days": days, "wind_speed_unit": "mph",
                "hourly": [
                    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
                ],
            },
            timeout=10
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


def fetch_ndbc_buoy(buoy_id: str) -> Optional[dict]:
    """Fetch the latest observation from a single NOAA NDBC buoy."""
    url = f"https://www.ndbc.noaa.gov/data/realtime2/{buoy_id}.txt"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        if len(lines) < 3:
            return None

        headers = lines[0].lstrip("#").split()
        values  = lines[2].split()

        def get(key, cast=float):
            try:
                idx = headers.index(key)
                v = values[idx]
                return None if v in ("MM", "99.0", "999", "9999") else cast(v)
            except (ValueError, IndexError):
                return None

        return {
            "buoy_id":       buoy_id,
            "wave_height_m": get("WVHT"),
            "wave_period_s": get("DPD"),
            "wave_dir_deg":  get("MWD"),
            "wind_speed_ms": get("WSPD"),
            "wind_dir_deg":  get("WDIR"),
            "water_temp_c":  get("WTMP"),
            "timestamp":     f"{values[0]}-{values[1]}-{values[2]} "
                             f"{values[3]}:{values[4]}",
        }
    except Exception as e:
        print(f"  ⚠️  Buoy {buoy_id} unavailable: {e}")
        return None


def fetch_all_buoys(buoy_ids: list[str]) -> list[dict]:
    """
    Fetch every buoy in the list. Returns only successful readings.
    Handles an empty buoy_ids list gracefully — returns [].
    """
    if not buoy_ids:
        print("  ℹ️  No buoys configured for this island.")
        return []

    results = []
    for bid in buoy_ids:
        print(f"  📡 Buoy {bid}...", end=" ", flush=True)
        data = fetch_ndbc_buoy(bid)
        if data and data.get("wave_height_m") is not None:
            print(f"✅  {data['wave_height_m']:.1f}m "
                  f"@ {data['wave_period_s'] or '?'}s")
            results.append(data)
        else:
            print("no usable data")

    return results


def summarise_buoys(buoys: list[dict]) -> Optional[dict]:
    """
    Produce a single consensus reading from 0, 1, or many buoy dicts.
    Uses safe_mean() throughout — never raises regardless of input.
    Returns None if buoys list is empty, so callers can branch cleanly.
    """
    if not buoys:
        return None

    avg_h = safe_mean([b.get("wave_height_m") for b in buoys])
    avg_p = safe_mean([b.get("wave_period_s")  for b in buoys])
    avg_d = safe_mean([b.get("wave_dir_deg")   for b in buoys])

    n = len(buoys)
    return {
        "count":        n,
        "avg_height_m": avg_h,
        "avg_period_s": avg_p,
        "avg_dir_deg":  avg_d,
        # Readable label so callers don't have to format this themselves
        "source_label": "1 buoy" if n == 1 else f"{n} buoys averaged",
    }


# ─────────────────────────────────────────────────────────────────
#  CONFIDENCE ENGINE
# ─────────────────────────────────────────────────────────────────

def build_confidence(model_hour: dict,
                     buoy_summary: Optional[dict],
                     hours_from_now: int) -> dict:
    """
    Cross-reference model forecast against buoy consensus.
    buoy_summary comes from summarise_buoys() and may be None —
    every branch handles that explicitly.
    """
    confidence = 1.0
    flags = []

    model_h = model_hour.get("swell_height_m") or 0
    model_p = model_hour.get("swell_period_s")  or 0

    # ── Temporal decay ────────────────────────────────────────────
    if hours_from_now > 72:
        penalty = min(0.35, (hours_from_now - 72) / 72 * 0.35)
        confidence -= penalty
        flags.append(f"{hours_from_now // 24}d forecast — directional guidance only")

    # ── Buoy cross-reference ──────────────────────────────────────
    # Only meaningful for the near-term window
    if buoy_summary and hours_from_now <= 12:
        buoy_h = buoy_summary.get("avg_height_m")   # may be None if buoy had no WVHT
        buoy_p = buoy_summary.get("avg_period_s")
        src    = buoy_summary["source_label"]

        if buoy_h is not None:
            delta_h = abs(model_h - buoy_h)
            if delta_h > 1.0:
                confidence -= 0.30
                flags.append(
                    f"model {model_h:.1f}m vs {src} {buoy_h:.1f}m "
                    f"({delta_h:+.1f}m) — treat with caution"
                )
            elif delta_h > 0.5:
                confidence -= 0.15
                flags.append(
                    f"model/buoy delta {delta_h:.1f}m ({src}) — slight disagreement"
                )
            else:
                flags.append(f"{src} confirms model ✓ ({buoy_h:.1f}m observed)")
        else:
            # Buoy replied but wave height field was missing/MM
            confidence -= 0.08
            flags.append(f"{src} online but no wave height reading")

        if buoy_p is not None and model_p and abs(model_p - buoy_p) > 3:
            confidence -= 0.15
            flags.append(
                f"period mismatch: model {model_p:.0f}s vs buoy {buoy_p:.0f}s"
            )

    elif buoy_summary is None:
        confidence -= 0.10
        flags.append("no buoy data — model only, confidence reduced")

    # ── Swell character ───────────────────────────────────────────
    if model_p and model_p < 8:
        confidence -= 0.10
        flags.append("wind swell — messy and harder to predict")
    elif model_p and model_p >= 14:
        flags.append("long-period groundswell — well organised")

    # ── Wind wave chop ratio ──────────────────────────────────────
    ww_h = model_hour.get("wind_wave_height_m") or 0
    if ww_h > 0 and model_h > 0 and (ww_h / max(model_h, 0.1)) > 0.7:
        confidence -= 0.10
        flags.append("high wind-wave component — choppy conditions likely")

    # ── Large swell timing uncertainty ───────────────────────────
    if model_h > 3.0:
        confidence -= 0.10
        flags.append("large swell — arrival timing may shift ±6-12 hrs")

    confidence = round(max(0.05, min(1.0, confidence)), 2)
    label = ("HIGH"   if confidence >= 0.75 else
             "MEDIUM" if confidence >= 0.50 else "LOW")

    return {
        "confidence": confidence,
        "label":      label,
        "flags":      flags,
        "agreement":  confidence >= 0.65,
    }


# ─────────────────────────────────────────────────────────────────
#  SPOT INFERENCE ENGINE
# ─────────────────────────────────────────────────────────────────

def direction_in_window(d: float, dmin: float, dmax: float) -> bool:
    if dmin <= dmax:
        return dmin <= d <= dmax
    return d >= dmin or d <= dmax      # window crosses 360 deg


def direction_score(d: float, ideal: float, dmin: float, dmax: float) -> float:
    if not direction_in_window(d, dmin, dmax):
        return 0.0
    diff = abs(d - ideal)
    if diff > 180:
        diff = 360 - diff
    half = max(abs(ideal - dmin), abs(ideal - dmax), 1)
    return round(max(0.0, 1.0 - diff / half), 3)


def score_period(p: float, pmin: float, pideal: float) -> float:
    if p < pmin:
        return round(max(0.0, (p / pmin) * 0.4), 3)
    if p >= pideal:
        return 1.0
    return round(0.4 + 0.6 * ((p - pmin) / (pideal - pmin)), 3)


def score_height(h: float, hmin: float, hideal: float, hmax: float) -> float:
    if h < hmin:    return round(max(0.0, h / hmin * 0.3), 3)
    if h > hmax:    return 0.0
    if h <= hideal: return round(0.3 + 0.7 * ((h - hmin)  / (hideal - hmin)), 3)
    return          round(1.0 - 0.5  * ((h - hideal) / max(hmax - hideal, 0.01)), 3)


def score_wind(wdir: float, wspd: float, spot: SpotConfig) -> float:
    if wspd > spot.wind_speed_max:
        return 0.1
    if direction_in_window(wdir, spot.wind_offshore_min, spot.wind_offshore_max):
        return round(max(0.5, 1.0 - (wspd / spot.wind_speed_max) * 0.5), 3)
    penalty = min(1.0, wspd / 15)
    return round(max(0.0, 0.4 - penalty * 0.4), 3)


def infer_spot(hour: dict, spot: SpotConfig) -> dict:
    """Infer surf conditions at a spot from offshore model data."""
    swell_dir = hour.get("swell_direction_deg") or 0
    swell_h   = (hour.get("swell_height_m") or 0) * spot.exposure
    swell_p   = hour.get("swell_period_s") or 0
    wind_dir  = hour.get("wind_direction_deg") or 0
    wind_spd  = hour.get("wind_speed_mph") or 0

    in_shadow = any(
        direction_in_window(swell_dir, smin, smax)
        for smin, smax in spot.shadow_zones
    )
    near_window = direction_in_window(
        swell_dir, spot.swell_dir_min - 25, spot.swell_dir_max + 25
    )
    is_wrap = in_shadow and near_window and spot.wrap_factor > 0 and swell_p >= 14

    if in_shadow and not is_wrap:
        s_dir   = 0.0
        swell_h = swell_h * 0.05        # diffraction only — essentially nothing
    elif is_wrap:
        s_dir   = spot.wrap_factor * 0.7
        swell_h = swell_h * spot.wrap_factor * 0.6
    else:
        s_dir = direction_score(swell_dir, spot.swell_dir_ideal,
                                spot.swell_dir_min, spot.swell_dir_max)

    s_period = score_period(swell_p, spot.swell_period_min, spot.swell_period_ideal)
    s_height = score_height(swell_h, spot.swell_height_min,
                            spot.swell_height_ideal, spot.swell_height_max)
    s_wind   = score_wind(wind_dir, wind_spd, spot)

    weights = {
        "beach": {"dir": 0.30, "period": 0.25, "height": 0.25, "wind": 0.20},
        "reef":  {"dir": 0.25, "period": 0.35, "height": 0.20, "wind": 0.20},
        "point": {"dir": 0.35, "period": 0.30, "height": 0.20, "wind": 0.15},
    }
    w = weights.get(spot.break_type, weights["beach"])
    overall = (s_dir    * w["dir"]    + s_period * w["period"] +
               s_height * w["height"] + s_wind   * w["wind"])

    label_map = [
        (0.85, "PUMPING"),
        (0.70, "GOOD"),
        (0.50, "FAIR"),
        (0.30, "POOR"),
        (0.00, "FLAT/BLOWN"),
    ]
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
        "score_overall":      round(overall, 3),
        "quality":            quality,
    }


# ─────────────────────────────────────────────────────────────────
#  OUTPUT FORMATTING
# ─────────────────────────────────────────────────────────────────

# Column width for the forecast table
W = 72

QUALITY_DISPLAY = {
    "PUMPING":    "◆◆◆◆  FIRING",
    "GOOD":       "◆◆◆◇  GOOD",
    "FAIR":       "◆◆◇◇  FAIR",
    "POOR":       "◆◇◇◇  POOR",
    "FLAT/BLOWN": "◇◇◇◇  FLAT / BLOWN OUT",
}

CONF_DISPLAY = {
    "HIGH":   "CONF: HIGH  ▓▓▓",
    "MEDIUM": "CONF: MED   ▓▓░",
    "LOW":    "CONF: LOW   ▓░░",
}


def deg_to_compass(deg: Optional[float]) -> str:
    if deg is None:
        return "---"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]


def fmt_note(inferred: dict, confidence: dict) -> str:
    """
    Build a single condensed note line from flags and spot state.
    Prioritises the most meaningful signal — keeps it to one line.
    """
    parts = []

    # Spot geometry first — these override everything
    if inferred["in_shadow"]:
        return "Swell blocked — wrong direction for this shore."
    if inferred["is_wrap_event"]:
        parts.append("Wrap event — long-period swell bending in, size reduced.")

    # Confidence flags — take the two most important, reword them tersely
    flags = confidence["flags"]
    # Filter out the "confirms model" positive flag — not a note-worthy warning
    warn_flags = [f for f in flags if "confirms" not in f]

    if warn_flags:
        # Truncate each flag to a short phrase
        short = []
        for f in warn_flags[:2]:
            # Clip at the em-dash or pipe if present, keep left side
            f = f.split("—")[0].split("|")[0].strip().rstrip(".")
            if len(f) > 52:
                f = f[:49] + "..."
            short.append(f)
        parts.append("  /  ".join(short))

    return "  /  ".join(parts) if parts else ""


def print_spot_header(spot: SpotConfig) -> None:
    print(f"\n{'─' * W}")
    print(f"  {spot.name.upper()}")
    print(f"  {spot.break_type.capitalize()} break  ·  {spot.shore.capitalize()}-facing  "
          f"·  Optimal: {deg_to_compass(spot.swell_dir_ideal)} swell  "
          f"@ {spot.swell_period_ideal:.0f}s+  "
          f"·  Offshore: {deg_to_compass(spot.wind_offshore_min)}–"
          f"{deg_to_compass(spot.wind_offshore_max)} wind")
    print(f"  Size range: {spot.swell_height_min:.1f}–{spot.swell_height_max:.1f}m  "
          f"·  Exposure: {spot.exposure:.1f}x")
    if spot.notes:
        print(f"  {spot.notes}")
    print(f"{'─' * W}")


def print_forecast_row(dt: datetime, hour: dict,
                       inferred: dict, confidence: dict) -> None:
    """Print one hour as a compact two-line block."""

    # ── Line 1: date/time + rating + confidence ───────────────────
    date_str  = dt.strftime("%a %d %b")
    time_str  = dt.strftime("%H:%M")
    quality   = QUALITY_DISPLAY.get(inferred["quality"], inferred["quality"])
    conf_str  = CONF_DISPLAY.get(confidence["label"], confidence["label"])

    print(f"  {date_str}  {time_str}    {quality:<22}  {conf_str}")

    # ── Line 2: swell + wind readings ────────────────────────────
    ht   = (f"{inferred['effective_height_m']:.1f}m"
            if not inferred["in_shadow"] else " --  ")
    per  = (f"{hour['swell_period_s']:.0f}s"
            if hour.get("swell_period_s") else "--")
    sdir = deg_to_compass(hour.get("swell_direction_deg"))
    wspd = (f"{hour['wind_speed_mph']:.0f}mph"
            if hour.get("wind_speed_mph") is not None else "---")
    wdir = deg_to_compass(hour.get("wind_direction_deg"))
    gust = (f"gusts {hour['wind_gusts_mph']:.0f}"
            if hour.get("wind_gusts_mph") is not None else "")

    swell_str = f"Swell {ht} @ {per}  {sdir}"
    wind_str  = f"Wind {wspd} {wdir}  {gust}"
    print(f"             {swell_str:<28}  {wind_str}")

    # ── Line 3: note (only if there's something worth saying) ────
    note = fmt_note(inferred, confidence)
    if note:
        print(f"             ! {note}")


def print_divider(light: bool = False) -> None:
    print("  " + ("·" * (W - 2) if light else "-" * (W - 2)))


# ─────────────────────────────────────────────────────────────────
#  FORECAST RUNNER
# ─────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────
#  JSON EXPORT  —  condensed daily readings for remote display
#
#  Output shape:
#    {
#      "island":    "Bermuda",
#      "generated": "2025-05-17T14:00:00+00:00",
#      "days": [
#        {
#          "date":      "2025-05-17",
#          "label":     "Today",
#          "spots": {
#            "south_shore": { <condensed reading> },
#            "north_shore": { <condensed reading> }
#          }
#        },
#        ...   ← next 3 days, same shape
#      ]
#    }
#
#  Each condensed reading averages all hourly scores for that day,
#  picks the modal quality label, and surfaces the best single hour
#  as a "peak" snapshot for display context.
# ─────────────────────────────────────────────────────────────────



def _condense_day_hours(
    hours: list[dict],
    spot: SpotConfig,
    buoy_summary: Optional[dict],
    now: datetime,
) -> dict:
    """
    Collapse a list of same-day hourly dicts into one condensed reading.
    Returns a display-ready dict suitable for JSON export.
    """
    if not hours:
        return {"quality": "NO DATA", "score_overall": 0.0}

    inferred_list   = []
    confidence_list = []
    quality_labels  = []

    for hour in hours:
        dt             = datetime.fromisoformat(hour["time"]).astimezone(timezone.utc)
        hours_from_now = max(0, int((dt - now).total_seconds() / 3600))
        inf            = infer_spot(hour, spot)
        conf           = build_confidence(hour, buoy_summary, hours_from_now)
        inferred_list.append(inf)
        confidence_list.append(conf)
        quality_labels.append(inf["quality"])

    # ── Averaged numeric scores ───────────────────────────────────
    avg_score   = safe_mean([i["score_overall"]      for i in inferred_list])
    avg_height  = safe_mean([i["effective_height_m"] for i in inferred_list])
    avg_conf    = safe_mean([c["confidence"]         for c in confidence_list])

    # ── Modal quality label (most common across the day) ─────────
    modal_quality = Counter(quality_labels).most_common(1)[0][0]

    # Confidence label from average numeric score
    avg_conf_label = ("HIGH"   if (avg_conf or 0) >= 0.75 else
                      "MEDIUM" if (avg_conf or 0) >= 0.50 else "LOW")

    # ── Peak hour (highest scoring hour of the day) ───────────────
    best_idx  = max(range(len(inferred_list)),
                    key=lambda i: inferred_list[i]["score_overall"])
    best_inf  = inferred_list[best_idx]
    best_conf = confidence_list[best_idx]
    best_hour = hours[best_idx]
    peak_dt   = datetime.fromisoformat(best_hour["time"]).astimezone(timezone.utc)

    # Collect the most important warning flags across the day (deduplicated)
    all_flags: list[str] = []
    for c in confidence_list:
        all_flags.extend(c["flags"])
    # Keep unique flags, strip verbose buoy-confirmation messages
    unique_flags = list(dict.fromkeys(
        f for f in all_flags if "confirms model" not in f
    ))[:3]   # cap at 3 for display headroom

    return {
        # ── Day-level averaged readings ───────────────────────────
        "quality":           modal_quality,
        "score_overall":     round(avg_score  or 0, 3),
        "avg_height_m":      round(avg_height or 0, 2),
        "confidence":        round(avg_conf   or 0, 2),
        "confidence_label":  avg_conf_label,

        # ── Spot geometry ─────────────────────────────────────────
        "spot_name":         spot.name,
        "shore":             spot.shore,
        "break_type":        spot.break_type,

        # ── Peak hour snapshot ────────────────────────────────────
        "peak": {
            "time":          peak_dt.strftime("%H:%M"),
            "score":         round(best_inf["score_overall"], 3),
            "quality":       best_inf["quality"],
            "swell_height_m":best_inf["effective_height_m"],
            "swell_period_s":best_hour.get("swell_period_s"),
            "swell_dir":     deg_to_compass(best_hour.get("swell_direction_deg")),
            "swell_dir_deg": best_hour.get("swell_direction_deg"),
            "wind_speed_mph":best_hour.get("wind_speed_mph"),
            "wind_dir":      deg_to_compass(best_hour.get("wind_direction_deg")),
            "wind_dir_deg":  best_hour.get("wind_direction_deg"),
            "wind_gusts_mph":best_hour.get("wind_gusts_mph"),
            "is_wrap":       best_inf["is_wrap_event"],
            "in_shadow":     best_inf["in_shadow"],
            "confidence":    round(best_conf["confidence"], 2),
        },

        # ── Flags (top warnings for the day, deduped) ─────────────
        "flags": unique_flags,
    }


def export_forecast_json(
    island_key: str,
    days_ahead: int = 3,
    output_path: Optional[str] = None,
) -> dict:
    """
    Build and optionally save a condensed daily JSON forecast.

    Args:
        island_key:  Key from ISLANDS dict  (e.g. "bermuda")
        days_ahead:  How many days after today to include (default 3)
        output_path: If given, writes JSON to this file path.
                     Pass None to return the dict only (no file written).

    Returns:
        The full forecast dict — always, regardless of output_path.
    """
    island, spots = ISLANDS[island_key]
    now           = datetime.now(timezone.utc)
    today         = now.date()

    # ── Fetch data (same sources as run_island_forecast) ─────────
    print(f"\n  [export] Fetching buoys...")
    buoys        = fetch_all_buoys(island.buoy_ids)
    buoy_summary = summarise_buoys(buoys)

    print(f"  [export] Fetching model ({days_ahead + 1} days)...")
    fetch_days = days_ahead + 2          # buffer so we always have full days
    forecast   = fetch_open_meteo(
        island.offshore_lat, island.offshore_lon,
        days=fetch_days, timezone=island.timezone,
    )
    if not forecast:
        raise RuntimeError("Could not fetch model data from Open-Meteo.")

    # ── Group hourly readings by calendar date (UTC) ──────────────
    by_date: dict[str, list[dict]] = {}
    for hour in forecast:
        date_key = hour["time"][:10]     # "YYYY-MM-DD"
        by_date.setdefault(date_key, []).append(hour)

    # Build the ordered list of dates: today + next N days
    target_dates = [
        str(today + __import__("datetime").timedelta(days=d))
        for d in range(days_ahead + 1)
    ]

    # ── Assemble per-day, per-spot condensed readings ─────────────
    days_out = []
    for idx, date_str in enumerate(target_dates):
        label = ("Today"    if idx == 0 else
                 "Tomorrow" if idx == 1 else
                 datetime.strptime(date_str, "%Y-%m-%d").strftime("%A"))

        day_hours = by_date.get(date_str, [])

        spot_readings = {}
        for spot_key, spot in spots.items():
            spot_readings[spot_key] = _condense_day_hours(
                day_hours, spot, buoy_summary, now
            )

        days_out.append({
            "date":  date_str,
            "label": label,
            "spots": spot_readings,
        })

    # ── Buoy metadata for the receiving device ────────────────────
    buoy_meta = None
    if buoy_summary:
        buoy_meta = {
            "source":        buoy_summary["source_label"],
            "avg_height_m":  buoy_summary.get("avg_height_m"),
            "avg_period_s":  buoy_summary.get("avg_period_s"),
            "avg_dir_deg":   buoy_summary.get("avg_dir_deg"),
        }

    payload = {
        "island":      island.name,
        "island_key":  island_key,
        "generated":   now.isoformat(timespec="seconds"),
        "buoy":        buoy_meta,
        "days":        days_out,
    }

    # ── Write to file if path given ───────────────────────────────
    if output_path:
        with open(output_path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"  [export] Written → {output_path}  "
              f"({len(days_out)} days, {len(spots)} spots)")

    return payload


def run_island_forecast(island_key: str, days: int = 5,
                        show_hours: int = 48,
                        good_threshold: float = 0.55) -> None:

    island, spots = ISLANDS[island_key]
    now = datetime.now(timezone.utc)

    # ── Header ────────────────────────────────────────────────────
    print(f"\n{'=' * W}")
    print(f"  {island.name.upper()}  —  SURF FORECAST")
    print(f"  Generated: {now.strftime('%a %d %b %Y  %H:%M UTC')}")
    print(f"  Offshore read point: {island.offshore_lat}°N, "
          f"{abs(island.offshore_lon)}°{'W' if island.offshore_lon < 0 else 'E'}")
    print(f"{'=' * W}")

    # ── 1. Buoy ground truth ──────────────────────────────────────
    print(f"\n  Fetching buoy data ({len(island.buoy_ids)} configured)...")
    buoys        = fetch_all_buoys(island.buoy_ids)
    buoy_summary = summarise_buoys(buoys)

    if buoy_summary:
        h_str = (f"{buoy_summary['avg_height_m']:.1f}m"
                 if buoy_summary["avg_height_m"] is not None else "n/a")
        p_str = (f"{buoy_summary['avg_period_s']:.0f}s"
                 if buoy_summary["avg_period_s"] is not None else "n/a")
        print(f"  Buoy ({buoy_summary['source_label']}):  "
              f"{h_str}  @  {p_str}  — used for confidence cross-check")
    else:
        print("  No buoy data available — all confidence ratings will be reduced.")

    # ── 2. Model forecast ─────────────────────────────────────────
    print(f"\n  Fetching Open-Meteo model forecast...")
    forecast = fetch_open_meteo(
        island.offshore_lat, island.offshore_lon,
        days=days, timezone=island.timezone
    )
    if not forecast:
        print("  ERROR: Could not fetch model data. Aborting.")
        return
    print(f"  Model: {len(forecast)} hourly readings  ({days} days)\n")

    # ── 3. Per-spot tables ────────────────────────────────────────
    for spot_key, spot in spots.items():

        print_spot_header(spot)

        best_windows   = []
        prev_day       = None

        for hour in forecast[:show_hours]:
            dt = datetime.fromisoformat(hour["time"])
            try:
                dt = dt.astimezone(timezone.utc)
            except Exception:
                pass

            hours_from_now = max(0, int((dt - now).total_seconds() / 3600))
            inferred       = infer_spot(hour, spot)
            confidence     = build_confidence(hour, buoy_summary, hours_from_now)

            # Day separator
            day_str = dt.strftime("%A %d %B")
            if day_str != prev_day:
                if prev_day is not None:
                    print_divider(light=True)
                print(f"\n  ── {day_str} ──")
                prev_day = day_str

            print_forecast_row(dt, hour, inferred, confidence)

            if inferred["score_overall"] >= good_threshold:
                best_windows.append((dt, inferred, confidence, hour))

        # ── Best windows summary ──────────────────────────────────
        print(f"\n{'─' * W}")
        if best_windows:
            print(f"  BEST WINDOWS  (score ≥ {good_threshold})\n")
            for dt, inf, conf, raw in best_windows:
                ht   = f"{inf['effective_height_m']:.1f}m"
                per  = f"{raw.get('swell_period_s', 0):.0f}s"
                sdir = deg_to_compass(raw.get("swell_direction_deg"))
                wspd = (f"{raw['wind_speed_mph']:.0f}mph"
                        if raw.get("wind_speed_mph") is not None else "---")
                wdir = deg_to_compass(raw.get("wind_direction_deg"))
                qual = QUALITY_DISPLAY.get(inf["quality"], inf["quality"])
                conf_s = CONF_DISPLAY.get(conf["label"], conf["label"])

                print(f"  {dt.strftime('%a %d %b  %H:%M')}   "
                      f"{qual:<22}  {conf_s}")
                print(f"                       "
                      f"Swell {ht} @ {per} {sdir}   Wind {wspd} {wdir}")

                note = fmt_note(inf, conf)
                if note:
                    print(f"                       ! {note}")
                print()
        else:
            print(f"  No windows above {good_threshold} threshold "
                  f"in the {days}-day forecast.")
        print(f"{'=' * W}")


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_island_forecast(
        island_key="fistral",
        days=5,
        show_hours=24,
        good_threshold=0.55,
    )
#    # data = export_forecast_json(
#     island_key="fistral",
#     days_ahead=3,
#     output_path="bermuda_forecast.json",
#     )
#     data = export_forecast_json("bermuda", days_ahead=3)
