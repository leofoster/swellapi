"""Per-spot calibration from surfers' session reports. Stateless: the app stores the reports
(with the forecast it showed at the time) and sends them here; nothing costs an Open-Meteo call.

Height: each report implies a scale = height_scale_used x observed / forecast. Take the median
in log space and shrink toward the current scale by n / (n + SHRINK), so one odd report can't
swing it. Direction: the rating-weighted mean swell direction of good sessions (4-5 stars),
shrunk the same way toward the current ideal.
"""
import math
from typing import Optional

FT = 0.3048
SHRINK = 3.0            # reports needed before calibration moves halfway to what they say
MIN_DIR_REPORTS = 3     # good sessions needed before suggesting a new ideal direction
SCALE_RANGE = (0.3, 3.0)


def _median(xs):
    xs = sorted(xs); n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _circ_mean(dirs, weights):
    sx = sum(w * math.sin(math.radians(d)) for d, w in zip(dirs, weights))
    sy = sum(w * math.cos(math.radians(d)) for d, w in zip(dirs, weights))
    return (math.degrees(math.atan2(sx, sy)) + 360) % 360, math.hypot(sx, sy) / max(sum(weights), 1e-9)


def _sdiff(a, b):
    return ((a - b + 540) % 360) - 180


def calibrate(reports: list[dict], current_scale: float = 1.0, current_ideal: Optional[float] = None) -> dict:
    notes = []
    # ── height ──
    logs = []
    for r in reports:
        obs = r.get("observed_surf_m")
        if obs is None and r.get("observed_surf_ft") is not None:
            obs = r["observed_surf_ft"] * FT
        fc = r.get("forecast_surf_m")
        if obs is None or not fc or fc < 0.1 or obs <= 0:
            continue                                       # flat days or missing values say nothing about scale
        used = r.get("height_scale_used") or current_scale
        logs.append(math.log(used * obs / fc))
    n = len(logs)
    scale = current_scale
    spread = None
    if n:
        k = n / (n + SHRINK)
        scale = math.exp(math.log(current_scale) + k * (_median(logs) - math.log(current_scale)))
        scale = min(max(scale, SCALE_RANGE[0]), SCALE_RANGE[1])
        if n >= 4:
            q = sorted(math.exp(x) for x in logs)
            spread = [round(q[n // 4], 2), round(q[(3 * n) // 4], 2)]
    else:
        notes.append("No usable height reports (need forecast_surf_m and an observed height above 0).")

    # ── direction ──
    good = [(r["swell_direction_deg"], r["rating"] - 3) for r in reports
            if r.get("rating") is not None and r["rating"] >= 4 and r.get("swell_direction_deg") is not None]
    ideal = current_ideal
    dir_n = len(good)
    if dir_n >= MIN_DIR_REPORTS:
        mean, agreement = _circ_mean([d for d, _ in good], [w for _, w in good])
        if current_ideal is None:
            ideal = mean
        else:
            ideal = (current_ideal + _sdiff(mean, current_ideal) * dir_n / (dir_n + SHRINK)) % 360
        if agreement < 0.5:
            notes.append("Good sessions came from scattered swell directions; the spot may work on a wide window.")
    elif dir_n:
        notes.append(f"{dir_n} good session(s) logged; need {MIN_DIR_REPORTS} before suggesting a direction.")

    # ── score bias (information only) ──
    pairs = [(r["forecast_score"], (r["rating"] - 1) / 4) for r in reports
             if r.get("forecast_score") is not None and r.get("rating") is not None]
    bias = round(sum(f - o for f, o in pairs) / len(pairs), 2) if pairs else None
    if bias is not None and abs(bias) >= 0.15 and len(pairs) >= 3:
        notes.append(f"Forecast quality reads {'high' if bias > 0 else 'low'} here by about {abs(bias) * 10:.0f}/10.")

    return {
        "height_scale": round(scale, 2),
        "height_reports_used": n,
        "observed_to_forecast_iqr": spread,
        "swell_dir_ideal": round(ideal) if ideal is not None else None,
        "direction_reports_used": dir_n,
        "score_bias": bias,
        "confidence": round(n / (n + SHRINK), 2),
        "notes": notes,
    }
