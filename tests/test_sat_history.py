"""Satellite history: pass selection, conditions matching, image endpoint (no network)."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

import api
import history as h

HOST = h.COG_HOST + "x/"


def item(i, when, tile=10):
    return {"id": f"S2A_28RDS_202611{i:02d}_0_L2A", "time": when, "satellite": "sentinel-2a",
            "tile_cloud_pct": tile, "tci": HOST + f"{i}/TCI.tif", "scl": HOST + f"{i}/SCL.tif"}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for c in (h._items, h._cloud, h._cond, h._img):
        c.clear()
    yield


def fake_scl(classes_by_item):
    """_window stub: SCL crop filled with the given class per item number (0 = no data)."""
    def win(href, lat, lon, size_m):
        n = int(href.split("/")[-2])
        if href.endswith("SCL.tif"):
            return np.full((1, 10, 10), classes_by_item[n], dtype="uint8")
        return np.full((3, 10, 10), 120, dtype="uint8")
    return win


COND = {"2026-11-15T12": {"swell_height_m": 1.1, "swell_period_s": 14.7, "tide_level": 0.2},
        "2026-11-12T12": {"swell_height_m": 2.0, "swell_period_s": 9.0, "tide_level": 0.8}}


def test_history_keeps_clear_passes_and_matches_the_hour(monkeypatch):
    found = [item(15, "2026-11-15T11:53:38Z"), item(14, "2026-11-15T11:53:40Z"),   # same pass, other tile
             item(12, "2026-11-12T11:53:43Z"), item(10, "2026-11-10T11:53:34Z")]
    monkeypatch.setattr(h, "search", lambda *a, **k: found)
    monkeypatch.setattr(h, "_window", fake_scl({15: 6, 14: 6, 12: 6, 10: 9}))     # 10: cloud over the spot
    monkeypatch.setattr(h, "conditions", lambda lat, lon: COND)
    out = h.history(28.13, -15.45, days=30, max_cloud=20)
    assert [p["id"][-10:-6] for p in out["passes"]] == ["1115", "1112"]           # duplicate + cloudy dropped
    assert out["passes"][0]["conditions"]["swell_period_s"] == 14.7
    assert out["passes"][0]["spot_cloud_pct"] == 0


def test_history_skips_no_data_and_honours_include_cloudy(monkeypatch):
    found = [item(15, "2026-11-15T11:53:38Z"), item(10, "2026-11-10T11:53:34Z")]
    monkeypatch.setattr(h, "search", lambda *a, **k: found)
    monkeypatch.setattr(h, "_window", fake_scl({15: 0, 10: 9}))                  # 15: spot off the tile
    monkeypatch.setattr(h, "conditions", lambda lat, lon: {})
    out = h.history(28.13, -15.45, include_cloudy=True)
    assert [p["spot_cloud_pct"] for p in out["passes"]] == [100]


def test_history_survives_open_meteo_failure(monkeypatch):
    monkeypatch.setattr(h, "search", lambda *a, **k: [item(15, "2026-11-15T11:53:38Z")])
    monkeypatch.setattr(h, "_window", fake_scl({15: 6}))

    def boom(lat, lon):
        raise h.surfdeck.UpstreamError("429")
    monkeypatch.setattr(h, "conditions", boom)
    out = h.history(28.13, -15.45)
    assert out["count"] == 1 and out["passes"][0]["conditions"] is None and out["notes"]


def test_endpoint_builds_https_image_urls(monkeypatch):
    monkeypatch.setattr(h, "search", lambda *a, **k: [item(15, "2026-11-15T11:53:38Z")])
    monkeypatch.setattr(h, "_window", fake_scl({15: 6}))
    monkeypatch.setattr(h, "conditions", lambda lat, lon: COND)
    r = TestClient(api.app).get("/spot/history", params={"lat": 28.13, "lon": -15.45},
                                headers={"x-forwarded-proto": "https", "host": "swellapi-1.onrender.com"})
    assert r.status_code == 200
    url = r.json()["passes"][0]["image_url"]
    assert url.startswith("https://swellapi-1.onrender.com/spot/history/image/S2A_28RDS_20261115_0_L2A?")


def test_image_endpoint(monkeypatch):
    h._items[item(15, "")["id"]] = item(15, "")
    monkeypatch.setattr(h, "_window", fake_scl({15: 6}))
    c = TestClient(api.app)
    r = c.get(f"/spot/history/image/{item(15, '')['id']}", params={"lat": 28.13, "lon": -15.45, "px": 128})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert "immutable" in r.headers["cache-control"]
    assert c.get("/spot/history/image/not-an-id", params={"lat": 1, "lon": 1}).status_code == 404


def test_catalogue_down_is_503(monkeypatch):
    def down(*a, **k):
        raise h.HistoryError("timeout")
    monkeypatch.setattr(h, "search", down)
    assert TestClient(api.app).get("/spot/history", params={"lat": 1, "lon": 1}).status_code == 503
