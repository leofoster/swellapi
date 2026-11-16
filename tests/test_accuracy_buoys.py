"""Angle loss, surf range, height_scale, and the raw buoy endpoints (no network)."""
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

import api
import buoys
import surfdeck as sd


def spot(**kw):
    base = dict(name="T", shore="w", swell_dir_min=250, swell_dir_max=340, swell_dir_ideal=295,
                swell_period_min=8, swell_period_ideal=14, swell_height_min=0.5, swell_height_ideal=1.5,
                swell_height_max=3.5, wind_offshore_min=90, wind_offshore_max=180, wind_speed_max=20,
                break_type="beach")
    base.update(kw)
    return sd.SpotConfig(**base)


def hour(d, h=1.0, p=10.0):
    return {"swell_height_m": h, "swell_peak_period_s": p, "swell_direction_deg": d,
            "wind_speed_mph": 3, "wind_direction_deg": 120}


def test_angled_swell_is_smaller_on_a_beach():
    square = sd.infer_spot_v2(hour(295), spot())["surf_height_m"]
    angled = sd.infer_spot_v2(hour(335), spot())["surf_height_m"]      # 40 deg off the beach normal
    assert angled < square * 0.95


def test_angle_loss_not_applied_to_points():
    a = sd.infer_spot_v2(hour(295), spot(break_type="point"))["surf_height_m"]
    b = sd.infer_spot_v2(hour(330), spot(break_type="point"))["surf_height_m"]
    assert a == b


def test_surf_range_and_height_scale():
    r = sd.infer_spot_v2(hour(295), spot())
    assert r["surf_min_m"] < r["surf_max_m"] == r["surf_height_m"]
    scaled = sd.infer_spot_v2(hour(295), spot(height_scale=0.5))
    assert abs(scaled["surf_height_m"] - r["surf_height_m"] * 0.5) < 0.02


def _ndbc_txt(now):
    t = now - timedelta(minutes=30)
    old = now - timedelta(hours=30)
    head = "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE\n"
    units = "#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft\n"
    row = lambda d, h: f"{d:%Y %m %d %H %M} 300 5.0 7.0 {h} 14 9.0 310 1015.0 22.0 24.0 MM MM MM MM\n"
    return head + units + row(t, 1.5) + row(old, 9.9)


def test_parse_newest_first_and_missing(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = buoys._parse(_ndbc_txt(now), buoys.MET, hours=24)
    assert len(rows) == 1                               # 30 h old row dropped
    assert rows[0]["wave_height_m"] == 1.5 and rows[0]["dewpoint_c"] is None


def test_endpoints_without_network(monkeypatch):
    st = {"id": "X1", "name": "Test buoy", "owner": "t", "lat": 21.0, "lon": -158.0}
    monkeypatch.setattr(sd, "nearest_buoys", lambda lat, lon, max_km=200, k=3, **kw:
                        [] if lat > 25 else [{**st, "distance_km": 12}][:k])
    monkeypatch.setattr(sd, "_stations", {"t": 1e18, "list": [st]})
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(buoys, "_get", lambda sid, kind, timeout: _ndbc_txt(now) if kind == "txt" else None)
    c = TestClient(api.app)

    j = c.get("/buoys/near", params={"lat": 21.1, "lon": -158.0}).json()
    b = j["buoys"][0]
    assert (b["lat"], b["lon"], b["distance_km"]) == (21.0, -158.0, 12)
    assert b["latest"]["wave_height_m"] == 1.5 and "met" not in b

    assert c.get("/buoys/near", params={"lat": 28.1, "lon": -15.4}).json()["count"] == 0

    d = c.get("/buoys/X1", params={"hours": 6}).json()
    assert d["name"] == "Test buoy" and len(d["met"]) == 1 and d["spectral"] == []
