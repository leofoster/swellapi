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

**Render (live API for the web app).** Shared-IP 429s (see "Home relay") plus
a 30-60s cold start. `/debug/upstream` shows the route in use and what
Open-Meteo says over it.

## Swell map (big-screen proof of concept)

The build writes `site/map/<region>.json` for `bermuda`, `newquay` and `global`:
swell height, direction and period on a coarse grid (2 degrees local, 10 degrees
global), one frame every 3 hours for 48 hours. The map is stylised; it only
needs to show a system coming. Maps rebuild every 6 hours (UTC 00/06/12/18) and
off-cycle runs re-publish the previous copy (`SITE_URL` in the workflow).

Open-Meteo counts every grid point as one call (confirmed by hitting the
600/minute limit). Cost: ~670 calls per map rebuild, ~2,700 a day. Free tier
allows 10,000 a day.

The simulator's **Swell map** button (or `m`) draws it like
`scripts/ocean_swell_map.py`: streamlines along the swell crests, coloured and
thickened by height. Coastline is Natural Earth land as one outline (110m
global, 50m local), loaded from the jsDelivr CDN, and lines are clipped to it.
Map failures are logged in `status.json` (`map_failed`) and never fail the build.

### Tweaking it

```bash
.venv/bin/python scripts/dev_server.py            # http://localhost:8090/sim/
.venv/bin/python scripts/dev_server.py --refresh  # re-fetch data first
```

`/sim/` is served straight from `simulator/`, so code edits show on refresh.
The **Map tuning** panel (visible while the map is on) changes line direction,
density, step size, line lengths, seeding, interpolation, coast reach, widths,
colours, colour scale, land style and timing live. "Copy settings" gives JSON
to paste into `MAP_DEFAULTS` in `simulator/index.html` to make it the default.

The readout is offshore swell. The OLED shows what reaches the chosen spot after
shadowing, so a sheltered spot can read 0.1m while the map shows 2m.

## Square display (5", design proof of concept)

Simulator **Panel → Square 5" 720×720**. Three pages, 9s each (map page runs
one full loop of frames); click the screen or Prev/Next to change page:

1. **Now:** spot, local time, live/stale dot, 0-10 score ring in the quality
   colour, swell and wind with direction arrows, next 24h strip with the best
   window tagged.
2. **Next 48 hours:** a bar per 3h block (height = swell, colour = quality),
   wind arrows and speeds, day dividers, best window callout.
3. **Swell map:** the streamline map (uses the Map tuning settings).

Data is the same `/esp/<island>` payload as the OLED, plus `map/<region>.json`.
Quality colours use the API's `quality_key` cut points. 720×720 is a
placeholder resolution until a panel is chosen; the layout is in canvas units.

## Home relay (temporary fix for 429s on Render)

Render's shared IP runs out of Open-Meteo's free quota. Until there is an API
key, the website's API can borrow your home connection:

```bash
python scripts/relay.py setup    # once: creates the switch (a secret gist), prints Render settings
python scripts/relay.py on       # relay + Cloudflare tunnel + keep-awake; switch ON
python scripts/relay.py status
python scripts/relay.py off      # switch OFF, stop everything
```

- Render needs two env vars, set once: `RELAY_CONFIG_URL` (printed by
  `setup`) and `PROXY_TOKEN` (from `.proxy.env`). Remove `OPEN_METEO_PROXY`.
- The API reads the switch every minute. GitHub caches raw gists for a few
  minutes, so on/off takes up to ~5 min to land.
- You can flip `"enabled"` by editing the gist on github.com.
- If the relay is unreachable (Mac asleep, tunnel restarted), the API falls
  back to calling Open-Meteo directly, so the worst case is today's behaviour.
- `scripts/dev_server.py` uses the local relay automatically when it is up,
  so experiments share its 30-minute cache.
- Everything shares your home IP's 10,000 calls/day. The real fix is an
  Open-Meteo API key.

## Not yet true

- Tides: `fetch_mock_tides` is synthetic (highs are always 3.5m). The web app's
  tide display was showing this mock data before it was switched off. It is off unless `MOCK_TIDES=1`, and
  never affects ratings by default.
- Buoy data (NDBC) is fetched by nothing; confidence is model-only.
- Firmware v2 has not been compiled or run on hardware yet. The simulator has.
