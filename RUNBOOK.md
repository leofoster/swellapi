# SURFMOD runbook

## The path

```
GitHub Actions (hourly)  -->  Open-Meteo        build_static.py scores the forecast
        |
        v
GitHub Pages (static JSON)  <--  ESP32 every 15 min, or the simulator in a browser
```

No server, PC or tunnel in the path. If a build fails, the last good site stays
live, and the device keeps showing what it has with a `!` until data recovers.

| # | Step | Fails how | Where it shows | Recovery |
|---|------|-----------|----------------|----------|
| 1 | Action calls Open-Meteo | 429 / timeout | Actions tab: red run, log names the reason | Usually clears next hour. If it persists, see "Open-Meteo refuses" |
| 2 | Action deploys Pages | Pages not enabled, permissions | Actions tab: `deploy` job red | Settings > Pages > Source: GitHub Actions |
| 3 | Device joins WiFi | Wrong SSID/pass, 5 GHz network | Screen: `wifi failed` | Fix `secrets.h`. ESP32 is 2.4 GHz only |
| 4 | Device fetches JSON | Wrong `API_URL` | Screen: `DNS fail` / `HTTP 404` | Check `API_URL` matches the Pages URL, path included |
| 5 | Data ages out | Builds failing for hours | Screen: `!` top right; after 48h, `Forecast expired` | Fix step 1 or 2 |

## First-time setup

1. Push to `main`. Settings > Pages > Source: **GitHub Actions**.
2. Actions tab > "Publish forecast" > Run workflow. When it's green, check
   `https://<user>.github.io/swellapi/esp/bermuda` returns JSON.
3. Firmware: copy `surf_oled/secrets_example.h` to `secrets.h`, set
   `API_URL "https://<user>.github.io/swellapi"`, flash `surf_oled.ino`.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q                              # scoring + API tests, no network
.venv/bin/uvicorn api:app --reload               # live API, simulator at http://localhost:8000/sim/
.venv/bin/python scripts/build_static.py         # build ./site exactly as CI does
python3 -m http.server 8090 -d site              # serve it; simulator at http://localhost:8090/sim/
```

The simulator runs the same logic and layout as the firmware. Change and test
there first (faults, clock warp, 128x32), then port the change to
`surf_oled.ino`. The two must stay in sync by hand.

A real ESP32 can point at your dev machine: `API_URL "http://<your-lan-ip>:8000"`.

## Known failure points

**Open-Meteo refuses.** The free tier limits requests per IP per day. GitHub's
runners are shared too, but each run is about 4 calls and runs land on
different machines. Unverified long term: if 429s become routine, the fix is an
Open-Meteo API key (paid), not another proxy.

**Scheduled runs pause.** GitHub disables schedules after 60 days with no repo
activity. Re-enable from the Actions tab.

**Schedule drift.** GitHub can delay cron runs by several minutes or skip one
under load. Data covers 48h, so a missed hour is invisible.

**Render (optional live API).** Same shared-IP 429 problem, plus a 30-60s cold
start. `/debug/upstream` shows what Open-Meteo says over the route the app
actually uses. `api_prox.py` is the old home-PC relay; it is not needed for the
default setup.

## Swell map (big-screen proof of concept)

The hourly build writes `site/map/<island>.json`: swell height, direction and
period on a ~1.5 degree grid, one frame every 3 hours for the next 48, from one
Open-Meteo request (land comes back null and doubles as the coastline). The
simulator's **Swell map** button (or `m`) draws it the way
`scripts/ocean_swell_map.py` does: streamlines along the swell crests, coloured
and thickened by height on one scale for all frames, stepping every 3 or 6
hours ("Map step"). Map failures are logged in `status.json` (`map_failed`)
but never fail the build.

The readout is offshore swell. The OLED shows what reaches the chosen spot after
shadowing, so a sheltered spot can read 0.1m while the map shows 2m.

Unverified: Open-Meteo may count each grid point against the daily quota
(~230 points per island per hour). If the build starts seeing 429s, coarsen
`MAP_REGIONS` in `swellmap.py` or build maps less often.

## Not yet true

- Tides: `fetch_mock_tides` is synthetic (highs are always 3.5m). The web app's
  tide display was showing this mock data before it was switched off. It is off unless `MOCK_TIDES=1`, and
  never affects ratings by default.
- Buoy data (NDBC) is fetched by nothing; confidence is model-only.
- Firmware v2 has not been compiled or run on hardware yet. The simulator has.
