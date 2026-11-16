"""Spot direction suggestions on synthetic coastlines (no network)."""
from fastapi.testclient import TestClient

import api
import coast


def use_world(monkeypatch, water):
    monkeypatch.setattr(coast, "_prefetch", lambda lat, lon: None)
    monkeypatch.setattr(coast, "is_water", lambda lat, lon, z: water(lat, lon))


def test_straight_north_facing_beach(monkeypatch):
    use_world(monkeypatch, lambda lat, lon: lat > 0.0)                  # sea to the north
    r = coast.suggest(0.001, 0.0)
    assert r["status"] == "ok"
    assert coast.ang_diff(r["facing_deg"], 0) <= 5 and coast.ang_diff(r["swell_dir_ideal"], 0) <= 5
    assert (r["wind_offshore_min"], r["wind_offshore_max"]) == (135, 225)
    assert r["confidence"] > 0.8 and r["shadow_zones"] == []


def test_offshore_island_makes_a_shadow(monkeypatch):
    # island 40-60 km out to the north-east
    island = lambda lat, lon: 0.3 < lat < 0.5 and 0.3 < lon < 0.5
    use_world(monkeypatch, lambda lat, lon: lat > 0.0 and not island(lat, lon))
    r = coast.suggest(0.001, 0.0)
    assert any(a <= 45 <= b for a, b in r["shadow_zones"]), r["shadow_zones"]
    assert r["fetch_km"]["45"] < 100


def test_pin_on_sand_is_moved_and_inland_refused(monkeypatch):
    use_world(monkeypatch, lambda lat, lon: lat > 0.0)
    r = coast.suggest(-0.001, 0.0)                                       # ~110 m inland
    assert r["status"] == "ok" and r["pin"]["moved_m"] > 0
    assert coast.suggest(-0.5, 0.0)["status"] == "inland"


def test_reef_is_lower_confidence(monkeypatch):
    use_world(monkeypatch, lambda lat, lon: lat > 0.0)
    assert coast.suggest(0.001, 0.0, "reef")["confidence"] < coast.suggest(0.001, 0.0)["confidence"]


def test_rasterise_fills_ring_and_leaves_hole():
    outer = [(0, 0), (4096, 0), (4096, 4096), (0, 4096)]
    hole = [(1024, 1024), (1024, 3072), (3072, 3072), (3072, 1024)]      # opposite winding
    m = coast._rasterise([outer, hole], 4096)
    px = lambda x, y: m[y * coast.RES + x]
    assert px(10, 10) == 1 and px(128, 128) == 0


def test_endpoint(monkeypatch):
    use_world(monkeypatch, lambda lat, lon: lat > 0.0)
    c = TestClient(api.app)
    j = c.get("/spot/suggest", params={"lat": 0.001, "lon": 0.0, "break_type": "beach"}).json()
    assert {"swell_dir_min", "swell_dir_max", "swell_dir_ideal", "wind_offshore_min",
            "wind_offshore_max", "shadow_zones"} <= j.keys()

    def boom(*a, **k):
        raise coast.CoastError("down")
    monkeypatch.setattr(coast, "suggest", boom)
    assert c.get("/spot/suggest", params={"lat": 1, "lon": 1}).status_code == 503
