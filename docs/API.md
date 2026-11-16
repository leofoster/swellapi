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

## Suggest a spot's directions: GET /spot/suggest

`GET /spot/suggest?lat=&lon=&break_type=beach|reef|point|rivermouth`

Works out the swell window and offshore wind from the coastline (OpenStreetMap ocean polygons
via OpenFreeMap; credit `attribution`). Use it for a "Calculate" button when creating a break,
then let the user edit the values.

- **Returns** `swell_dir_min`, `swell_dir_max`, `swell_dir_ideal`, `shadow_zones`,
  `wind_offshore_min`, `wind_offshore_max`: these drop straight into `POST /forecast`.
- **Also** `facing_deg` (the way the shore faces), `confidence` (0-1), `notes`, `pin` (where it
  measured; a pin on the sand is moved up to 1 km onto the water) and `fetch_km` (open water per
  bearing, for drawing the window).
- **`status`**: `ok`, `inland` (no sea within 1 km), `open-water` (no coast within 1 km) or
  `enclosed` (every seaward direction meets land). Only `ok` has direction fields.
- **503** if the map tiles can't be reached. Takes about 0.5-2 s; tiles are cached for a day.
- **How it works:** facing = mean direction of water on rings out to 3 km; a ray every 5° across
  the seaward half measures open water to 150 km; bearings with too little open water are outside
  the window, or shadow zones if inside it; offshore wind = facing + 180 ± 45.
- **Reefs and points: mark the peel.** Put the pin on the peak and add `peel_deg` (the compass
  bearing the wave peels toward, e.g. from a drag on the map) or `hand=left|right`. The reef is
  taken to run along the peel line, and the ideal swell is set round from up the line (20° for
  reefs, 45° for points) so the wave peels. Returns `reef_facing_deg`, `peel_deg`, `hand`.
  On 8 known reefs and points this cut the average ideal-direction error from ~60° to ~35°
  (rough reference values; unverified).
- **Accuracy (42 test breaks, compared with approximate known facings, unverified):** beaches
  usually within ~20°; reefs and points often 30-130° off, because their best swell depends on
  the seabed, not the shoreline. Reef and point results carry lower `confidence` and a note.
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

## Session reports: POST /spot/calibrate

Surfers log what it was actually like; the API turns a spot's reports into a better
`height_scale` and `swell_dir_ideal`. **The API stores nothing**: the app keeps the reports and
sends them all (up to 500) when it wants a recalibration. No Open-Meteo calls.

Each report (all fields optional; send what you have):

| Field | What |
|---|---|
| `time` | When (ISO) |
| `observed_surf_ft` or `observed_surf_m` | Height of the sets, as the surfer judged it |
| `rating` | 1-5, how good it was |
| `forecast_surf_m` | `surf_height_m` the app showed for that hour (**save it with the report**) |
| `forecast_score` | `score_overall` shown for that hour |
| `swell_direction_deg` | `swell_direction_deg` shown for that hour |
| `height_scale_used` | The spot's `height_scale` at the time |

Request: `{"reports": [...], "height_scale": <current>, "swell_dir_ideal": <current>}`.

Response: `height_scale` and `swell_dir_ideal` to save back to the spot, `height_reports_used`,
`direction_reports_used`, `confidence` (0-1), `observed_to_forecast_iqr` (spread, 4+ reports),
`score_bias` (forecast score minus rating on a 0-1 scale; information only) and `notes`.

How: each report implies a scale (`used × observed / forecast`); the median moves the current
value by `n / (n + 3)`, so one odd report can't swing it. A new ideal direction needs 3+ sessions
rated 4-5. Flat days (observed 0) are ignored for height.

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

## Satellite history: GET /spot/history

Past Sentinel-2 passes over a spot, each with a true-colour image and the conditions modelled for
that hour, so you can see what a given swell, wind and tide actually did there.

- `GET /spot/history?lat=&lon=&days=60&max_cloud=20&limit=12&size_m=2400` (pin the break itself;
  `days` up to 92; `max_cloud` is % cloud over the crop, not the whole tile).
- Returns `passes`, newest first: `id`, `time` (UTC), `satellite`, `spot_cloud_pct`,
  `tile_cloud_pct`, `image_url`, `image_path`, and `conditions`: `wave_height_m`,
  `swell_height_m`, `swell_period_s` (peak), `swell_direction_deg`/`swell_direction`,
  `wind_speed_mph`, `wind_gusts_mph`, `wind_direction_deg`/`wind_direction`, `tide_height_m`,
  `tide_level` (0 low to 1 high), `tide_status`. `conditions` is null if Open-Meteo is unavailable.
- `GET /spot/history/image/{id}?lat=&lon=&size_m=2400&px=720`: JPEG crop (~100 kB), north up,
  centred on the pin. Immutable: cache it forever.
- Source: Copernicus Sentinel-2 L2A COGs on AWS (Earth Search). Free, no key, no rate limit we
  depend on. 10 m pixels: whitewater lines and sandbars show, wave faces don't.
- Passes are around 11:00-12:00 UTC (Canaries), every 2-5 days; trade-wind cloud removes many.
  La Cícer, 60 days to 2026-11-16: 16 passes, 3 under 20% cloud at the spot.
- Speed: first list ~10 s (cloud check per pass), then cached; each new image ~4-5 s.
- Attribution required: "Contains modified Copernicus Sentinel data".

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
