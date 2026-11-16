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
def no_buoys(monkeypatch):
    monkeypatch.setattr(api, "buoy_correction", lambda rows, lat, lon, now: (rows, None))


@pytest.fixture(autouse=True)
def stub_upstream(monkeypatch):
    monkeypatch.setattr(api, "fetch_forecast", lambda lat, lon: (fake_forecast(lat, lon), "Atlantic/Bermuda"))


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
    monkeypatch.setattr(api, "fetch_forecast", lambda lat, lon: (fake_forecast(lat, lon, today), "Atlantic/Bermuda"))
    c = TestClient(api.app)
    assert c.get("/health").status_code == 200
    assert c.get("/esp/bermuda?blocks=2").json()["v"] == 2
    assert c.get("/esp/atlantis").status_code == 404
    assert "spots" in c.get("/islands").json()["islands"]["bermuda"]
    assert c.get("/sim/").status_code == 200


def test_upstream_error_is_502_with_reason(monkeypatch):
    def boom(lat, lon):
        raise surfdeck.UpstreamError("weather: HTTP 429: Daily API request limit exceeded")
    monkeypatch.setattr(api, "fetch_forecast", boom)
    r = TestClient(api.app).get("/esp/bermuda")
    assert r.status_code == 502
    assert "429" in r.json()["detail"]


def test_map_grid_parsing(monkeypatch):
    import swellmap

    class Resp:
        status_code = 200
        def __init__(self, n): self.n = n
        def json(self):
            return [{"hourly": {"time": [f"2026-07-01T{h:02d}:00" for h in range(4)],
                                "swell_wave_height": [None] * 4 if k == 0 else [1.234, 1.5, 1.6, 1.7],
                                "swell_wave_direction": [None] * 4 if k == 0 else [350.4, 10, 20, 30],
                                "swell_wave_period": [None] * 4 if k == 0 else [9.04, 9, 9, 9]}}
                    for k in range(self.n)]

    region = swellmap.MAP_REGIONS["bermuda"]
    ny = len(swellmap._frange(*region["lat"], region["step"]))
    nx = len(swellmap._frange(*region["lon"], region["step"]))
    monkeypatch.setattr(swellmap.surfdeck._session, "get", lambda *a, **k: Resp(nx * ny))
    g = swellmap.fetch_swell_grid("bermuda")
    assert len(g["h"]) == 2 and g["step_h"] == 3 and len(g["h"][0]) == ny and len(g["h"][0][0]) == nx
    assert g["h"][0][0][0] is None            # first point is land
    assert g["h"][0][0][1] == 1.23 and g["d"][0][0][1] == 350
    assert g["times"][1] - g["times"][0] == 3 * 3600         # 3-hourly frames
    assert g["h"][1][0][1] == 1.7


APP_BODY = {   # shape the Base44 web app sends
    "spot_name": "My Reef", "lat": 32.3, "lon": -64.8,
    "swell_dir_min": 135, "swell_dir_max": 260, "swell_dir_ideal": 180,
    "swell_period_min": 10, "swell_period_ideal": 16,
    "swell_height_min": 0.8, "swell_height_ideal": 2, "swell_height_max": 4.5,
    "wind_offshore_min": 315, "wind_offshore_max": 60, "wind_speed_max": 20,
    "break_type": "rivermouth", "exposure": 1, "shadow_zones": [], "wrap_factor": 0,
    "days": 7, "timezone": "auto", "tide_level_ideal": "mid-low", "tide_movement_ideal": "rising",
}


def _client_now(monkeypatch):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    monkeypatch.setattr(api, "fetch_forecast", lambda lat, lon: (fake_forecast(lat, lon, today), "Atlantic/Bermuda"))
    return TestClient(api.app)


def test_web_app_request_shape(monkeypatch):
    r = _client_now(monkeypatch).post("/forecast", json=APP_BODY)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["timezone"] == "Atlantic/Bermuda"          # "auto" resolved to the spot's zone
    assert len(d["days"]) == 7
    day, hour = d["days"][0], d["days"][0]["hours"][0]
    assert day["quality_key"] in ("flat", "poor", "poor_to_fair", "fair", "fair_to_good", "good", "epic")
    assert hour["quality_key"] == hour["quality_key"].lower()
    assert hour["tide_height_m"] is None and day["high_tides"] == []   # no tide data, still 200


def test_tide_failure_does_not_fail_forecast(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("tide provider down")
    monkeypatch.setattr(api, "get_tides", broken)
    assert _client_now(monkeypatch).post("/forecast", json=APP_BODY).status_code == 200


def test_post_esp_for_saved_spot(monkeypatch):
    r = _client_now(monkeypatch).post("/esp?blocks=4", json=APP_BODY)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["s"] == "My Reef" and len(d["b"]) == 4 and "e" in d["b"][0]


def test_relay_switch_and_fallback(monkeypatch):
    calls = []

    class R:
        def __init__(self, code, body): self.status_code, self._b, self.text = code, body, ""
        def json(self): return self._b

    switch = {"enabled": True, "url": "https://relay.example/"}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if url == "https://switch.example/relay.json":
            return R(200, switch)
        if url.startswith("https://relay.example"):
            raise surfdeck.requests.ConnectionError("relay down")
        return R(200, {"hourly": {}})

    monkeypatch.setattr(surfdeck, "RELAY_CONFIG_URL", "https://switch.example/relay.json")
    monkeypatch.setattr(surfdeck, "RELAY_CONFIG_TTL", 0)
    monkeypatch.setattr(surfdeck, "OPEN_METEO_PROXY", "")
    monkeypatch.setattr(surfdeck._session, "get", fake_get)

    assert surfdeck.relay_base() == "https://relay.example"
    assert surfdeck.om_get("weather", {}, "t") == {"hourly": {}}        # relay down -> direct
    assert calls[-2].startswith("https://relay.example/forecast")
    assert calls[-1] == surfdeck.DIRECT_URLS["weather"]

    switch["enabled"] = False
    assert surfdeck.relay_base() is None
