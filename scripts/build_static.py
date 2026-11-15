"""Build the static forecast site that the ESP32 and the simulator read.

    python scripts/build_static.py [--out site]

Writes, per island and per spot:
    site/esp/<island>              ESP payload for the island's default spot (no extension:
                                   same path the live API serves, so firmware needs no change)
    site/esp/<island>.json         same, with an extension for browsers
    site/esp/spots/<island>/<spot>.json  per-spot payloads (own dir: a file and a
                                   directory cannot share the name esp/<island>)
    site/forecast/<island>.json    full hourly forecast, default spot
    site/map/<island>.json         swell grid for the next 12h (ambient map)
    site/islands.json
    site/sim/                      the simulator

Each island costs two Open-Meteo calls per run (results are cached per location).
Exits non-zero if any island fails, so a scheduled run never publishes a partial site
and the last good deploy stays live.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import esp_forecast, island_forecast, list_islands  # noqa: E402
from surfdeck import ISLANDS  # noqa: E402
from swellmap import MAP_REGIONS, MAP_HOURS, fetch_swell_grid  # noqa: E402

import requests  # noqa: E402

# Where the last build is published. Set by the workflow; maps are reused from
# here between scheduled rebuilds so Open-Meteo is not asked every hour.
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

BLOCKS = 16  # 48h. Devices drop blocks that are already in the past, so a stale file still works.


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")))


def published_map(region_key: str):
    if not SITE_URL:
        return None
    try:
        r = requests.get(f"{SITE_URL}/map/{region_key}.json", timeout=20)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def build_map(region_key: str, every_h: int, now: datetime):
    """Rebuild on the region's cadence (UTC hour divisible by every_h); otherwise
    reuse the published copy while it still covers the next day."""
    if now.hour % every_h:
        old = published_map(region_key)
        if old and old.get("times") and old["times"][-1] - now.timestamp() > (MAP_HOURS - every_h - 2) * 3600:
            return old, "reused"
    return fetch_swell_grid(region_key), "fetched"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "site"))
    out = Path(ap.parse_args().out)
    if out.exists():
        shutil.rmtree(out)

    now = datetime.now(timezone.utc)
    failed = []
    for island_key, (_, spots) in ISLANDS.items():
        try:
            default = esp_forecast(island_key, BLOCKS, now=now).model_dump()
            write(out / "esp" / island_key, default)
            write(out / "esp" / f"{island_key}.json", default)
            for spot_key in spots:
                write(out / "esp" / "spots" / island_key / f"{spot_key}.json",
                      esp_forecast(island_key, BLOCKS, spot_key, now=now).model_dump())
            write(out / "forecast" / f"{island_key}.json",
                  island_forecast(island_key, days=3, now=now).model_dump())
            print(f"ok    {island_key}: {len(default['b'])} blocks, {len(spots)} spot(s)")
        except Exception as e:  # HTTPException carries the upstream reason in .detail
            failed.append(island_key)
            print(f"FAIL  {island_key}: {getattr(e, 'detail', e)}", file=sys.stderr)

    # Maps are a nice-to-have: a failure is logged but does not fail the build.
    map_failed = []
    for region_key, region in MAP_REGIONS.items():
        try:
            data, how = build_map(region_key, region["every_h"], now)
            write(out / "map" / f"{region_key}.json", data)
            print(f"ok    map {region_key} ({how})")
        except Exception as e:
            map_failed.append(region_key)
            print(f"WARN  map {region_key}: {e}", file=sys.stderr)

    write(out / "islands.json", list_islands())
    write(out / "status.json", {"generated": now.isoformat(timespec="seconds"), "failed": failed,
                                     "map_failed": map_failed})
    shutil.copytree(ROOT / "simulator", out / "sim")
    (out / "index.html").write_text('<meta http-equiv="refresh" content="0; url=sim/">')
    (out / ".nojekyll").write_text("")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
