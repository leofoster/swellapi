from surfdeck import ISLANDS, deg_to_compass, direction_in_window, infer_spot

SOUTH = ISLANDS["bermuda"][1]["south_shore"]


def hour(**kw):
    base = dict(swell_direction_deg=180, swell_height_m=2.0, swell_period_s=16,
                wind_direction_deg=0, wind_speed_mph=5)
    base.update(kw)
    return base


def test_direction_window_wraps_north():
    assert direction_in_window(350, 300, 60)
    assert direction_in_window(10, 300, 60)
    assert not direction_in_window(180, 300, 60)


def test_compass():
    assert deg_to_compass(0) == "N"
    assert deg_to_compass(359) == "N"
    assert deg_to_compass(225) == "SW"
    assert deg_to_compass(None) == "---"


def test_ideal_south_swell_scores_high():
    r = infer_spot(hour(), SOUTH)
    assert r["quality"] in ("GOOD", "PUMPING")
    assert not r["in_shadow"]


def test_shadowed_swell_is_flat():
    # WNW swell cannot reach the south shore. The old island endpoint dropped
    # shadow_zones and rated this POOR at full height.
    r = infer_spot(hour(swell_direction_deg=289, swell_period_s=7), SOUTH)
    assert r["in_shadow"]
    assert r["effective_height_m"] < 0.2
    assert r["quality"] == "FLAT/BLOWN"


def test_onshore_gale_kills_score():
    calm = infer_spot(hour(), SOUTH)["score_overall"]
    blown = infer_spot(hour(wind_direction_deg=180, wind_speed_mph=30), SOUTH)["score_overall"]
    assert blown < calm - 0.15


def test_scores_bounded():
    for d in range(0, 360, 15):
        r = infer_spot(hour(swell_direction_deg=d, swell_height_m=9, swell_period_s=25), SOUTH)
        assert 0.0 <= r["score_overall"] <= 1.0
