# Forecast API contract

One scoring engine, two clients: the SwellCraft web app and the ESP32 companion.
Interactive schema: `/docs` on any running instance.

## POST /forecast

Scores every hour against a spot profile. Used by the web app.

Request (JSON):

| Field | Type | Notes |
|---|---|---|
| `spot_name` | string | |
| `lat`, `lon` | number | |
| `swell_dir_min`, `swell_dir_max`, `swell_dir_ideal` | degrees | window may wrap through north (min > max) |
| `swell_period_min`, `swell_period_ideal` | s | defaults 8, 14 |
| `swell_height_min`, `swell_height_ideal`, `swell_height_max` | m | defaults 0.5, 1.5, 3.5 |
| `wind_offshore_min`, `wind_offshore_max` | degrees | offshore window, may wrap |
| `wind_speed_max` | mph | default 20 |
| `break_type` | `beach` \| `reef` \| `point` \| `rivermouth` | sets score weights |
| `exposure` | number | multiplies swell height, default 1.0 |
| `shadow_zones` | `[[from_deg, to_deg], ...]` | swell from these directions is blocked |
| `wrap_factor` | 0-1 | how much long-period swell wraps into shadow zones |
| `tide_level_ideal` | `low` \| `mid-low` \| `mid` \| `mid-high` \| `high` \| null | optional (`low-mid` accepted) |
| `tide_movement_ideal` | `rising` \| `falling` \| null | optional |
| `days` | 1-7 | default 3 |
| `timezone` | IANA zone or `"auto"` | `"auto"` uses the spot's own zone. Default `UTC` |

Response:

```
{ spot_name, lat, lon,
  timezone,          // zone actually used ("auto" resolved)
  generated,         // UTC ISO
  tides_are_mock,    // false in production; tide fields below are then null / []
  days: [ { date, day_label,                    // local to `timezone`
            quality_key, modal_quality,
            avg_score, avg_height_m,
            peak_hour, peak_quality, peak_quality_key, peak_score,
            high_tides: [], low_tides: [],
            hours: [ { time,                     // ISO with local offset
                       hour_label, hours_from_now,
                       swell_height_m, swell_period_s, swell_direction_deg, swell_direction_compass,
                       wind_speed_mph, wind_direction_deg, wind_direction_compass, wind_gusts_mph,
                       tide_height_m, tide_status, score_tide_mod,      // null without tide data
                       score_direction, score_period, score_height, score_wind,
                       score_overall,            // 0-1
                       quality,                  // legacy uppercase: PUMPING GOOD FAIR POOR FLAT/BLOWN
                       quality_key,              // flat poor poor_to_fair fair fair_to_good good epic
                       effective_height_m, in_shadow, is_wrap, confidence, confidence_label } ] } ] }
```

Scores are 0-1. `quality_key` cut points: epic >= 0.85, good >= 0.70,
fair_to_good >= 0.60, fair >= 0.50, poor_to_fair >= 0.40, poor >= 0.30, else flat.

Errors: `422` invalid request (body names the field), `502` Open-Meteo failed
(`detail` gives the reason, e.g. a 429 quota message). Tide problems never fail
a request.

## Device payload

`POST /esp?blocks=8` takes the same body as `/forecast` and returns the compact
3-hour-block payload the ESP32 reads. `GET /esp/{island}` does the same for the
built-in islands, and is also published statically (see RUNBOOK.md).

```
{ v: 2, s: spot name, g: generated ISO, gt: generated unix,
  b: [ { t: "Mon 15" (local), e: block start unix, hn, h, p, sd, sg, w, wd, wg,
         r: 0-1, q: PUMPING|GOOD|FAIR|POOR|FLAT } ] }
```

## Other

- `GET /forecast/{island}?days=&spot=`: built-in island spots
- `GET /map/{island}`: swell grid for the map (also static at `map/<island>.json`)
- `GET /islands`, `GET /health`, `GET /debug/upstream`

## Changes the web app should make

1. Send `timezone: "auto"` (it hardcodes `UTC` today).
2. Key quality badges on `quality_key` instead of lowercasing `quality`.
3. Treat tide fields as optional (null / empty) and hide the tide UI when absent.
4. Stop presenting tide values cached before this change: they came from a mock generator.
