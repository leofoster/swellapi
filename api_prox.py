# api_prox.py — residential proxy for Open-Meteo
#
# Open-Meteo's free tier rejects calls from Render's shared datacenter IPs, so
# the deployed API routes its upstream requests through this process running on
# a home connection instead:
#
#     ESP32 -> Render (api.py) -> this proxy -> Open-Meteo
#
# Run it:
#     python api_prox.py
#
# Expose it (it must be reachable from Render, so localhost is not enough):
#     cloudflared tunnel --url http://localhost:8000      # stable-ish, free
#     ngrok http 8000                                     # URL rotates on restart
#
# Then on Render set:
#     OPEN_METEO_PROXY = https://<your-tunnel-host>
#     PROXY_TOKEN      = <same value as below, if you set one>
#
# NOTE: a public tunnel makes this reachable by anyone who finds the URL. Set
# PROXY_TOKEN to require a matching X-Proxy-Token header so it is not an open
# relay for someone else's traffic against your IP's rate limit.

import os
import time
import threading

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

UPSTREAM = {
    "marine":   "https://marine-api.open-meteo.com/v1/marine",
    "forecast": "https://api.open-meteo.com/v1/forecast",
}

PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")        # "" disables the check
CACHE_TTL   = float(os.environ.get("PROXY_CACHE_TTL", "900"))   # 15 min
PORT        = int(os.environ.get("PORT", "8000"))

# Identical params inside the TTL are served from memory. The ESP polls every
# 15 min and each poll costs two upstream calls, so this keeps the home IP well
# clear of the same free-tier limits that broke the datacenter path.
_cache: dict[tuple, tuple[float, int, dict]] = {}
_lock = threading.Lock()

app = FastAPI(title="Open-Meteo residential proxy")


def _check_token(request: Request) -> None:
    if PROXY_TOKEN and request.headers.get("X-Proxy-Token") != PROXY_TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing X-Proxy-Token")


def _passthrough(kind: str, request: Request) -> JSONResponse:
    _check_token(request)

    # multi_items(), not dict(): surfdeck sends "hourly" as a repeated query
    # param (hourly=wave_height&hourly=swell_wave_height&...). dict() keeps only
    # the last one, so the proxy would silently request a single field and the
    # caller would blow up on the missing keys.
    params = request.query_params.multi_items()
    key    = (kind, tuple(sorted(params)))
    now    = time.monotonic()

    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            age = int(now - hit[0])
            print(f"  cache hit  {kind} (age {age}s)")
            return JSONResponse(hit[2], status_code=hit[1],
                                headers={"X-Proxy-Cache": f"hit;age={age}"})

    try:
        resp = requests.get(UPSTREAM[kind], params=params, timeout=20)
    except Exception as e:
        print(f"  upstream FAILED {kind}: {type(e).__name__}: {e}")
        # 502 so the caller can tell "proxy could not reach upstream" apart
        # from "upstream said no".
        return JSONResponse({"error": True, "reason": f"proxy upstream error: {e}"},
                            status_code=502)

    try:
        body = resp.json()
    except Exception:
        print(f"  non-JSON from {kind}: {resp.status_code} {resp.text[:120]}")
        return JSONResponse({"error": True, "reason": "upstream returned non-JSON",
                             "status": resp.status_code, "snippet": resp.text[:200]},
                            status_code=502)

    print(f"  {kind} -> {resp.status_code}")

    # Only cache good responses; caching a rate-limit error would keep serving
    # it for the whole TTL.
    if resp.status_code == 200 and not body.get("error"):
        with _lock:
            _cache[key] = (now, resp.status_code, body)

    # Pass the real status through instead of flattening everything to 200.
    return JSONResponse(body, status_code=resp.status_code)


@app.get("/marine")
def marine(request: Request):
    return _passthrough("marine", request)


@app.get("/forecast")
def forecast(request: Request):
    return _passthrough("forecast", request)


@app.get("/health")
def health():
    with _lock:
        entries = len(_cache)
    return {"status": "ok", "cached_entries": entries,
            "cache_ttl_s": CACHE_TTL, "token_required": bool(PROXY_TOKEN)}


if __name__ == "__main__":
    print(f"proxy on 0.0.0.0:{PORT}  cache_ttl={CACHE_TTL}s  "
          f"token={'on' if PROXY_TOKEN else 'OFF (open relay)'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
