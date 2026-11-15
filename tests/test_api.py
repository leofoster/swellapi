from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api
import surfdeck

NOW = datetime(2026, 7, 1, 13, 20, tzinfo=timezone.utc)   # 10:20 in Bermuda (UTC-3 in summer)


START = datetime(2026, 7, 1, tzinfo=timezone.utc)


def fake_forecast(lat, lon, start=None):
    start = start or START
    return [{
        "time": (start + timedelta(hours=i)).isoformat(),
        "swell_height_m": 1.8, "swell_direction_deg": 185.0, "swell_period_s": 14.0,
        "swell_peak_period_s": 15.0, "wave_height_m": 2.0, "wind_wave_height_m": 0.3,
        "wind_wave_period_s": 4.0, "wind_speed_mph": 6.0, "wind_direction_deg": 10.0, "wind_gusts_mph": 9.0,
    } for i in range(7 * 24)]


@pytest.fixture(autouse=True)
def stub_upstream(monkeypatch):
    monkeypatch.setattr(api, "fetch_open_meteo", fake_forecast)


def test_esp_blocks_use_local_labels_and_epochs():
    r = api.esp_forecast("bermuda", blocks=4, now=NOW)
    assert len(r.b) == 4
    first = r.b[0]
    assert first.e == int(datetime(2026, 7, 1, 13, tzinfo=timezone.utc).timestamp())
    assert first.t == "Wed 10"               # local, not UTC "Wed 13"
    assert first.hn == 0
    assert r.b[1].e - first.e == 3 * 3600
    assert r.gt == int(NOW.timestamp())


def test_island_forecast_groups_by_local_day():
    f = api.island_forecast("bermuda", days=2, now=NOW)
    assert f.timezone == "Atlantic/Bermuda"
    assert [d.date for d in f.days] == ["2026-07-01", "2026-07-02"]
    assert f.days[0].hours[0].time.startswith("2026-07-01T00:00:00-03:00")
    # fake data ends at 2026-07-08 00:00 UTC = 07-07 21:00 local; day 2 must be complete
    assert len(f.days[1].hours) == 24


def test_spot_selection_and_unknown_spot():
    north = api.island_forecast("bermuda", days=1, spot_key="north_shore", now=NOW)
    assert north.spot_name == "North Rock"
    with pytest.raises(api.HTTPException) as e:
        api.island_forecast("bermuda", spot_key="nope", now=NOW)
    assert e.value.status_code == 404


def test_mock_tides_off_by_default():
    f = api.island_forecast("bermuda", days=1, now=NOW)
    assert f.tides_are_mock is False
    assert all(h.score_tide_mod == 0 for h in f.days[0].hours)


def test_http_endpoints(monkeypatch):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    monkeypatch.setattr(api, "fetch_open_meteo", lambda lat, lon: fake_forecast(lat, lon, today))
    c = TestClient(api.app)
    assert c.get("/health").status_code == 200
    assert c.get("/esp/bermuda?blocks=2").json()["v"] == 2
    assert c.get("/esp/atlantis").status_code == 404
    assert "spots" in c.get("/islands").json()["islands"]["bermuda"]
    assert c.get("/sim/").status_code == 200


def test_upstream_error_is_502_with_reason(monkeypatch):
    def boom(lat, lon):
        raise surfdeck.UpstreamError("weather: HTTP 429: Daily API request limit exceeded")
    monkeypatch.setattr(api, "fetch_open_meteo", boom)
    r = TestClient(api.app).get("/esp/bermuda")
    assert r.status_code == 502
    assert "429" in r.json()["detail"]
