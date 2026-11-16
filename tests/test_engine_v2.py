from datetime import datetime, timedelta, timezone

import pytest

import surfdeck as sd
from surfdeck import ISLANDS, infer_spot

SOUTH = ISLANDS["bermuda"][1]["south_shore"]   # window 135-260, ideal 180, shadow 260-315, offshore wind 315-60


def hour(**kw):
    base = dict(swell_height_m=1.5, swell_period_s=12, swell_direction_deg=290,      # shadowed WNW swell
                swell2_height_m=None, swell2_period_s=None, swell2_direction_deg=None,
                wind_speed_mph=8, wind_direction_deg=0, wind_gusts_mph=10)
    base.update(kw)
    return base


def test_secondary_swell_counts():
    # The real 2026-11-15 case: big WNW swell (shadowed) plus a 0.86m S swell.
    h = hour(swell2_height_m=0.9, swell2_period_s=12, swell2_direction_deg=183)
    v1, v2 = infer_spot(h, SOUTH, engine="v1"), infer_spot(h, SOUTH, engine="v2")
    assert v1["effective_height_m"] < 0.1                  # v1 only saw the shadowed WNW swell
    assert v2["effective_height_m"] > 0.9
    assert v2["score_overall"] > v1["score_overall"] + 0.2
    assert v2["dominant_swell_dir"] == 183


def test_longer_period_surfs_bigger():
    short = infer_spot(hour(swell_direction_deg=180, swell_period_s=7), SOUTH)
    long_ = infer_spot(hour(swell_direction_deg=180, swell_period_s=16), SOUTH)
    assert long_["effective_height_m"] > short["effective_height_m"]
    assert long_["surf_height_m"] > short["surf_height_m"]


def test_breaking_height_formula():
    assert sd.breaking_height(1.0, 10) == pytest.approx(1.55, abs=0.02)
    assert sd.breaking_height(0, 10) == 0


def test_direction_falls_off_smoothly():
    # window starts at 135 (no shadow zone on that side)
    inside_edge, just_outside, far = (infer_spot(hour(swell_direction_deg=d), SOUTH)["score_direction"] for d in (137, 132, 90))
    assert abs(inside_edge - just_outside) < 0.15          # no cliff at the window edge
    assert far == 0


def test_glassy_any_direction():
    onshore_light = sd.score_wind_v2(180, 3, 4, SOUTH)
    onshore_mod = sd.score_wind_v2(180, 15, 18, SOUTH)
    offshore_mod = sd.score_wind_v2(0, 12, 14, SOUTH)
    assert onshore_light >= 0.9
    assert onshore_mod < 0.3 < offshore_mod


def test_gusts_penalised():
    assert sd.score_wind_v2(0, 12, 25, SOUTH) < sd.score_wind_v2(0, 12, 14, SOUTH)


def test_tides_from_sea_level():
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    import math
    rows = [{"time": (t0 + timedelta(hours=i)).isoformat(),
             "sea_level_m": 0.4 * math.sin(2 * math.pi * i / 12.42)} for i in range(72)]
    t = sd.tides_from_sea_level(rows, timezone.utc)
    day = t["extremes"]["2026-07-02"]
    assert len(day["high"]) == 2 and len(day["low"]) == 2
    assert all(abs(x["height_m"] - 0.4) < 0.05 for x in day["high"])
    h = t["hourly"]["2026-07-01T03"]                     # just after the first peak (~3.1h)
    assert h["level"] > 0.9


def test_v1_still_available_via_api(monkeypatch):
    from fastapi.testclient import TestClient
    import api
    from tests.test_api import APP_BODY, fake_forecast
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    monkeypatch.setattr(api, "fetch_forecast", lambda lat, lon: (fake_forecast(lat, lon, today), "Atlantic/Bermuda"))
    monkeypatch.setattr(api, "buoy_correction", lambda rows, lat, lon, now: (rows, None))
    c = TestClient(api.app)
    assert c.post("/forecast", json={**APP_BODY, "days": 1}).json()["engine"] == "v2"
    assert c.post("/forecast", json={**APP_BODY, "days": 1, "engine": "v1"}).json()["engine"] == "v1"
    assert c.post("/forecast", json={**APP_BODY, "days": 1, "engine": "v9"}).status_code == 422


def test_buoy_failures_are_graceful(monkeypatch):
    rows = [{"time": datetime(2026, 7, 1, h, tzinfo=timezone.utc).isoformat(), "swell_height_m": 1.0,
             "wave_height_m": 1.2} for h in range(6)]
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)

    def boom(*a, **k):
        raise sd.requests.ConnectionError("NOAA down")
    monkeypatch.setattr(sd._session, "get", boom)            # station list unreachable
    monkeypatch.setattr(sd, "_stations", {"t": -1e9, "list": []})
    assert sd.buoy_correction(rows, 0.0, 0.0, now) == (rows, None)

    monkeypatch.setattr(sd, "nearest_buoys", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad xml")))
    assert sd.buoy_correction(rows, 0.0, 0.0, now) == (rows, None)   # any internal error -> no correction


def test_buoy_correction_decays(monkeypatch):
    rows = [{"time": datetime(2026, 7, 1, h, tzinfo=timezone.utc).isoformat(), "swell_height_m": 1.0,
             "swell2_height_m": None, "wave_height_m": 2.0} for h in range(24)]
    now = datetime(2026, 7, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(sd, "nearest_buoys", lambda *a, **k: [{"id": "X", "distance_km": 10}])
    monkeypatch.setattr(sd, "fetch_ndbc_buoy", lambda *a, **k: {"wave_height_m": 1.5, "timestamp": "t"})
    monkeypatch.setattr(sd, "_buoy_cache", {})
    out, info = sd.buoy_correction(rows, 0, 0, now)
    assert info["ratio"] == 0.75
    assert out[0]["swell_height_m"] == pytest.approx(0.75)
    assert out[6]["swell_height_m"] == pytest.approx(0.875)
    assert out[12]["swell_height_m"] == pytest.approx(1.0)
    assert rows[0]["swell_height_m"] == 1.0                  # input not mutated


def test_inland_point_is_refused(monkeypatch):
    class R:
        status_code = 200
        text = ""
        def __init__(self, body): self._b = body
        def json(self): return self._b
    snapped = {"latitude": 29.0, "longitude": -94.0, "timezone": "America/Chicago", "hourly": {"time": []}}
    monkeypatch.setattr(sd, "om_get", lambda kind, params, label, timeout=None: snapped)
    monkeypatch.setattr(sd, "_cache", {})
    with pytest.raises(sd.NoCoverageError):
        sd.fetch_forecast(38.5, -98.0)            # Kansas -> Gulf of Mexico grid cell
    with pytest.raises(sd.NoCoverageError):
        sd.fetch_forecast(38.5, -98.0)            # cached refusal, no second upstream call


def test_land_cell_with_no_wave_data_is_refused(monkeypatch):
    body = {"latitude": 38.54, "longitude": -98.04, "timezone": "America/Chicago",
            "hourly": {"time": ["2026-07-01T00:00", "2026-07-01T01:00"],
                       **{k: [None, None] for k in ("wave_height", "wave_direction", "wave_period", "swell_wave_height",
                                                    "swell_wave_direction", "swell_wave_period", "swell_wave_peak_period",
                                                    "wind_wave_height", "wind_wave_period", "wind_speed_10m",
                                                    "wind_direction_10m", "wind_gusts_10m")}}}
    monkeypatch.setattr(sd, "om_get", lambda kind, params, label, timeout=None: body)
    monkeypatch.setattr(sd, "_cache", {})
    with pytest.raises(sd.NoCoverageError, match="land"):
        sd.fetch_forecast(38.5, -98.0)
