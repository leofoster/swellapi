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
| `engine` | `v2` \| `v1` \| null | default `v2`; `v1` = original scoring, for comparison |
| `timezone` | IANA zone or `"auto"` | `"auto"` uses the spot's own zone. Default `UTC` |

Response:

```
{ spot_name, lat, lon,
  timezone,          // zone actually used ("auto" resolved)
  generated,         // UTC ISO
  engine,            // "v2" or "v1"
  tide_source,       // "open-meteo-model" | "mock" | null (no tide data)
  tides_are_mock,    // true only with MOCK_TIDES=1
  buoy,              // v2: { station, distance_km, observed_wave_m, model_wave_m, ratio, observed_at } or null
  model_grid_km,     // distance from the pin to the ocean model cell used
  days: [ { date, day_label,                    // local to `timezone`
            quality_key, modal_quality,
            avg_score, avg_height_m,
            peak_hour, peak_quality, peak_quality_key, peak_score,   // peak = best DAYLIGHT hour
            high_tides: ["12:20 (0.47m)"], low_tides: [...],     // local times; [] without tide data
            hours: [ { time,                     // ISO with local offset
                       hour_label, hours_from_now,
                       swell_height_m, swell_period_s, swell_direction_deg, swell_direction_compass,
                       swell_peak_period_s,      // PEAK period: what surfers / Surfline quote. Display this.
                       period_source,            // model-peak | gfs-wave | estimated (mean x 1.35)
                       secondary_swell_height_m, secondary_swell_period_s, secondary_swell_peak_period_s,
                       secondary_swell_direction_deg, secondary_swell_direction_compass,
                       surf_height_m,            // v2: breaking-height estimate, for display
                       daylight,                 // true / false / null (unknown)
                       wind_speed_mph, wind_direction_deg, wind_direction_compass, wind_gusts_mph,
                       tide_height_m,            // sea level vs mean, m (model)
                       tide_status,              // rising | falling
                       tide_level,               // 0 = local low, 1 = local high
                       score_tide_mod,           // null without tide data
                       score_direction, score_period, score_height, score_wind,
                       score_overall,            // 0-1
                       quality,                  // legacy uppercase: PUMPING GOOD FAIR POOR FLAT/BLOWN
                       quality_key,              // flat poor poor_to_fair fair fair_to_good good epic
                       effective_height_m, in_shadow, is_wrap, confidence, confidence_label } ] } ] }
```

Scores are 0-1. `quality_key` cut points: epic >= 0.85, good >= 0.70,
fair_to_good >= 0.60, fair >= 0.50, poor_to_fair >= 0.40, poor >= 0.30, else flat.

Errors: `422` invalid request (body names the field) or no ocean wave data at the
pin (inland; `detail` says so), `502` Open-Meteo failed
(`detail` gives the reason, e.g. a 429 quota message). Tide problems never fail
a request.

## Engine v2 (default)

Works for any coordinate with ocean wave data nearby. What changed from v1:

- Every swell counts: primary and secondary, combined by energy. v1 ignored
  the secondary swell, so a spot facing it could read FLAT.
- Period-aware height: longer periods surf bigger (equivalent height scaled to a
  10s reference, so existing height preferences keep their meaning).
  `surf_height_m` adds a breaking-height estimate for display.
- Direction falls off smoothly outside the swell window (no cliff at the edge).
- Light wind (5 mph or less) scores well from any direction; gusty wind costs points.
- Best windows (`peak_hour`, ESP "best") are daylight hours only.
- Tides from Open-Meteo's modelled sea level, scored against the local tidal range.
  Coarse model, not harbour tide tables: check against local tables before relying on it.
- Optional buoy correction: if an NOAA NDBC moored buoy is within 200 km, the next
  12h of swell is nudged toward its current reading (`buoy` says which). No buoy,
  NOAA down, or a slow response (4 s budget) simply means no correction.
- A pin on the beach snaps to the nearest sea cell; inland points return 422.

Expect v2 scores to run higher than v1 where secondary swells exist (in a
worldwide spot check, +0.3 to +1.1 on the 0-10 average).

## Swell period: mean vs peak

`swell_period_s` is the model's **mean** period. Surfers, Surfline and buoys' "dominant
period" use the **peak** period, typically 3-5 s longer for groundswell. Display
`swell_peak_period_s`. Engine v2 scores and sizes with the peak period; v1 still uses the mean.

Peak period source, best first: the model's native peak period (only some regions), NOAA
GFS-Wave's swell period when its direction agrees with the main model's (within 45 deg),
otherwise mean x 1.35. Checked against 8 NOAA buoys on 2026-11-16: mean absolute error vs
buoy peak period 3.3 s -> 1.9 s overall, about 3.9 s -> 1.2 s at groundswell buoys. Buoys
dominated by local wind chop (dominant period under 9 s) don't compare like for like.

## Surf height: angle loss, range and calibration

- **Angle loss (beach, rivermouth):** a swell arriving at angle θ to the beach normal spreads
  along the shore, so its height is scaled by `sqrt(cos θ)`. The beach normal is taken to be
  `swell_dir_ideal`, so **set `swell_dir_ideal` to the direction the beach faces**. Points and
  reefs are unchanged (they wrap and focus swell).
- **Range:** `surf_min_m` and `surf_max_m` (0.7× and 1.0× of `surf_height_m`). Show
  "2-3 ft" from these rather than a single number.
- **`height_scale`** (request, default 1.0, max 3): per-spot calibration. If a spot reliably reads
  20% high, send 0.8.
- **Unverified:** the 0.7-1.0 range and the angle loss are calibrated against one beach
  observation (La Cícer, 2026-11-16), not a dataset.

## Buoys: positions and raw readings

Source: NOAA NDBC realtime feeds. Coverage is mostly US waters plus some partner buoys;
elsewhere the list is empty (not an error).

- `GET /buoys/near?lat=&lon=&max_km=300&k=5&hours=0`: nearest buoys, each with `id`, `name`,
  `owner`, exact `lat`/`lon`, `distance_km`, `bearing_deg` (from the spot), `observed_at`,
  `latest` (newest non-null value of every field) and `available`. `hours>0` adds `met` and
  `spectral` history.
- `GET /buoys/{id}?hours=24`: one buoy with position, `latest`, `met` (wind, waves, pressure,
  temperatures) and `spectral` (the buoy's own swell vs wind-wave split: `swell_height_m`,
  `swell_period_s`, `swell_dir`, `wind_wave_*`, `steepness`). 404 for an unknown id.
- `units` maps every field to its unit. Missing readings are `null`.
- `POST /forecast` `buoy` (when a buoy corrected the forecast) now includes `name`, `lat`, `lon`.

## Device payload

`POST /esp?blocks=8` takes the same body as `/forecast` and returns the compact
3-hour-block payload the ESP32 reads. `GET /esp/{island}` does the same for the
built-in islands, and is also published statically (see RUNBOOK.md).

```
{ v: 2, s: spot name, g: generated ISO, gt: generated unix,
  b: [ { t: "Mon 15" (local), e: block start unix, hn, h, p, sd, sg, w, wd, wg,
         r: 0-1, q: PUMPING|GOOD|FAIR|POOR|FLAT,          // p is the PEAK period
         dl: daylight (bool|null), sh: surf height estimate m (v2) } ] }
```

## Other

- `GET /forecast/{island}?days=&spot=`: built-in island spots
- `GET /map/{island}`: swell grid for the map (also static at `map/<island>.json`)
- `GET /islands`, `GET /health`, `GET /debug/upstream`

## Changes the web app should make (engine v2 release)

1. Show `surf_height_m` ("surf ~1.2 m") next to swell height; it's what surfers expect.
2. Show the secondary swell when present (height, period, direction).
3. Re-enable the tide UI: `tide_height_m`, `tide_status`, `high_tides`, `low_tides` are
   populated again (`tide_source: "open-meteo-model"`). Label it as modelled.
4. Handle the new 422 for inland pins: show `detail` on the map-pick step.
5. Optional: `engine: "v1"` side by side for a while, to sanity-check the new scores.

## Changes the web app should make (earlier)

1. Send `timezone: "auto"` (it hardcodes `UTC` today).
2. Key quality badges on `quality_key` instead of lowercasing `quality`.
3. Treat tide fields as optional (null / empty) and hide the tide UI when absent.
4. Stop presenting tide values cached before this change: they came from a mock generator.
