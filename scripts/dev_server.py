"""Local playground for the simulator and swell map.

    python scripts/dev_server.py            # build data once if missing, serve on :8090
    python scripts/dev_server.py --refresh  # re-fetch forecast + maps first (~700 Open-Meteo calls)

Serves the built data from ./site, but /sim/ comes straight from ./simulator,
so edits to simulator/index.html show on browser refresh with no rebuild.
Open http://localhost:8090/sim/ then press "Swell map".
"""
import argparse
import functools
import http.server
import os
import subprocess
import urllib.request
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE, SIM = ROOT / "site", ROOT / "simulator"


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean == "/sim" or clean.startswith("/sim/"):
            rel = clean[len("/sim"):].lstrip("/") or "index.html"
            return str(SIM / rel)
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def relay_env() -> dict:
    """Use the local api_prox.py relay (port from .proxy.env) when it is running,
    so experiments share its cache instead of spending Open-Meteo quota."""
    env = dict(os.environ)
    cfg = ROOT / ".proxy.env"
    if not cfg.exists():
        return env
    vals = dict(line.split("=", 1) for line in cfg.read_text().split() if "=" in line)
    url = f"http://localhost:{vals.get('PORT', '8001')}"
    try:
        urllib.request.urlopen(f"{url}/health", timeout=2)
    except Exception:
        return env
    print(f"using relay {url}")
    env.update(OPEN_METEO_PROXY=url, PROXY_TOKEN=vals.get("PROXY_TOKEN", ""))
    return env


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-fetch data before serving")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    if args.refresh or not (SITE / "map").is_dir():
        print("building data into ./site ...")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_static.py"), "--out", str(SITE)],
                       check=False, env=relay_env())

    handler = functools.partial(Handler, directory=str(SITE))
    print(f"serving http://localhost:{args.port}/sim/  (Ctrl+C to stop)")
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
