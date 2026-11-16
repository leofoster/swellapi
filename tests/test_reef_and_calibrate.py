"""Peel marking for reefs/points, and session-report calibration."""
from fastapi.testclient import TestClient

import api
import calibrate as calib
import coast


def north_sea(monkeypatch):
    monkeypatch.setattr(coast, "_prefetch", lambda lat, lon: None)
    monkeypatch.setattr(coast, "is_water", lambda lat, lon, z: lat > 0.0)


def test_left_reef_wants_swell_from_up_the_line(monkeypatch):
    north_sea(monkeypatch)                     # shore faces N; surfer faces S; a left peels east
    r = coast.suggest(0.001, 0.0, "reef", hand="left")
    assert r["hand"] == "left" and r["peel_deg"] == 90
    assert r["swell_dir_ideal"] == 340          # 20 deg round toward the west, where it peels from
    assert coast.suggest(0.001, 0.0, "point", hand="right")["swell_dir_ideal"] == 45


def test_peel_bearing_sets_reef_facing(monkeypatch):
    north_sea(monkeypatch)
    r = coast.suggest(0.001, 0.0, "reef", peel_deg=120)   # reef angled: runs WNW-ESE
    assert r["reef_facing_deg"] == 30 and r["hand"] == "left"


def test_calibrate_height_shrinks_toward_current():
    reps = [{"forecast_surf_m": 1.2, "observed_surf_ft": 2.0}]             # reads ~2x high
    one = calib.calibrate(reps)["height_scale"]
    many = calib.calibrate(reps * 12)["height_scale"]
    assert 0.5 < many < one < 1.0              # implied 0.51; shrinkage keeps it a little above


def test_calibrate_uses_scale_at_report_time():
    reps = [{"forecast_surf_m": 1.0, "observed_surf_m": 1.0, "height_scale_used": 0.5}] * 30
    assert abs(calib.calibrate(reps, current_scale=1.0)["height_scale"] - 0.5) < 0.05


def test_calibrate_direction_and_bias():
    reps = [{"rating": 5, "swell_direction_deg": 350, "forecast_score": 0.9},
            {"rating": 4, "swell_direction_deg": 10, "forecast_score": 0.9},
            {"rating": 5, "swell_direction_deg": 0, "forecast_score": 0.9},
            {"rating": 1, "swell_direction_deg": 270, "forecast_score": 0.9}]
    r = calib.calibrate(reps, current_ideal=300)
    assert 320 < r["swell_dir_ideal"] < 360 and r["direction_reports_used"] == 3
    assert r["score_bias"] > 0.15 and r["height_reports_used"] == 0


def test_calibrate_endpoint():
    c = TestClient(api.app)
    j = c.post("/spot/calibrate", json={"height_scale": 1.0,
               "reports": [{"forecast_surf_m": 1.2, "observed_surf_ft": 2}] * 6}).json()
    assert j["height_scale"] < 1 and j["height_reports_used"] == 6
    assert c.post("/spot/calibrate", json={"reports": [{"rating": 9}]}).status_code == 422
