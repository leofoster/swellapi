# SURFMOD runbook — ESP32 surf display

Four systems, three handoffs, and one of them is a laptop. Written down because
when the screen goes blank the failure could be at any hop.

## The path

Trace it as Rufus, the ESP32 on the shelf, waking up to ask what the surf is
doing:

```
Rufus (ESP32)  --https-->  Render (api.py)  --https-->  ngrok  -->  your PC (api_prox.py)  -->  Open-Meteo
   every 15 min              /esp/bermuda         tunnel            15-min cache
```

| # | Step | Owner | Fails how | Recovery |
|---|------|-------|-----------|----------|
| 1 | Rufus joins wifi | Rufus | Screen: `wifi failed` | Check SSID/pass in `secrets.h`; ESP32 is 2.4GHz only, it cannot see a 5GHz-only SSID |
| 2 | Rufus resolves + calls Render | Rufus | Screen: `DNS fail` / `TLS fail` / `HTTP -1` | `API_HOST` wrong. Scheme and trailing slash are tolerated; a typo is not |
| 3 | Render runs the forecast | Render | Screen: `HTTP 502` | Hop 4 or 5 is down. Go to `/debug/upstream` |
| 4 | Render reaches your PC via ngrok | You | 502, `/debug/upstream` shows an exception | ngrok URL rotated, or PC asleep. See "ngrok URL rotated" |
| 5 | Proxy calls Open-Meteo | Your PC | 502, upstream `reason` names a limit | Rate limited even from home. Raise `PROXY_CACHE_TTL` |
| 6 | Rufus parses ~950 bytes | Rufus | Screen: `json ...` | Hit an ngrok HTML interstitial or a truncated body. Check `/esp/bermuda?blocks=8` in a browser |

## Bring it up

Terminal 1, on your PC:

```bash
export PROXY_TOKEN=<pick-a-secret>
python api_prox.py                  # listens on 0.0.0.0:8000
```

Terminal 2:

```bash
ngrok http 8000                     # copy the https://....ngrok-free.dev URL
```

Render dashboard, Environment, then let it redeploy:

```
OPEN_METEO_PROXY = https://<the-ngrok-host>      # no trailing slash
PROXY_TOKEN      = <the same secret>
```

## Verify, in this order

Stop at the first one that fails; that names the broken hop.

```bash
curl localhost:8000/health                                    # 1. proxy alive
curl -H "X-Proxy-Token: $PROXY_TOKEN" \
  "https://<ngrok-host>/marine?latitude=32.3&longitude=-64.8&hourly=swell_wave_height&forecast_days=1"
                                                              # 2. tunnel + token
curl https://swellapi-cayi.onrender.com/debug/upstream        # 3. Render -> Open-Meteo
curl https://swellapi-cayi.onrender.com/esp/bermuda?blocks=8  # 4. what Rufus sees
```

Then Rufus: Arduino IDE, Serial Monitor, 115200. It prints the URL, the
resolved IP, `TLS ok`, and any failure reason.

## Known failure points

**ngrok URL rotated.** Free ngrok issues a new hostname every restart, and
Render is holding the old one. Symptom: everything worked yesterday, 502 today.
Fix: copy the new URL into `OPEN_METEO_PROXY` and redeploy. This will keep
happening; a reserved domain on a paid plan or a different tunnel is the only
real fix.

**PC asleep or proxy not running.** Same 502. The whole design depends on your
machine being awake. Disable sleep on the machine, or accept the outage.

**Render free tier cold start.** First request after ~15 min idle takes 30-60s.
Rufus allows 60s and retries after a minute, so a cold start looks like one
blank cycle, not a permanent failure.

**Proxy cache is memory only.** Restarting the proxy drops it and the next
request goes upstream. Not a fault, just don't expect the cache to survive.

## Escape hatch

To cut the PC and ngrok out entirely, clear `OPEN_METEO_PROXY` on Render. Calls
go straight to Open-Meteo, which is the original behaviour — and which 502s from
Render's datacenter IPs. Only useful for confirming the proxy is the variable.

## Not yet true

Tides come from `fetch_mock_tides` — a synthetic generator, not real tide data.
Nothing on Rufus's screen shows tides today, but `/forecast` returns them and
they should not be trusted.
