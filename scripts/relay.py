"""Turn the home Open-Meteo relay on and off.

    python scripts/relay.py setup    # once: create the GitHub switch (a secret gist)
    python scripts/relay.py on       # start relay + tunnel + keep-awake, flip switch on
    python scripts/relay.py off      # flip switch off, stop everything
    python scripts/relay.py status

The switch is a secret gist holding {"enabled": bool, "url": "<tunnel>"}. The API
(Render) reads it via RELAY_CONFIG_URL, so the relay can come and go, and the
tunnel URL can change, without touching Render. You can also flip "enabled" by
editing the gist on github.com. If the relay is unreachable the API calls
Open-Meteo directly.

Needs: gh (logged in), cloudflared (brew install cloudflared).
Local state lives in .relay/ and .proxy.env (both gitignored).
"""
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".relay"
CFG = STATE / "config.json"
ENV_FILE = ROOT / ".proxy.env"
GIST_FILE = "relay.json"


# ── helpers ─────────────────────────────────────────────────────────────────
def load_env() -> dict:
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"PROXY_TOKEN={secrets.token_urlsafe(24)}\nPROXY_CACHE_TTL=1800\nPORT=8001\n")
        ENV_FILE.chmod(0o600)
    return dict(l.split("=", 1) for l in ENV_FILE.read_text().split() if "=" in l)


def load_cfg() -> dict:
    return json.loads(CFG.read_text()) if CFG.exists() else {}


def save_cfg(cfg: dict) -> None:
    STATE.mkdir(exist_ok=True)
    CFG.write_text(json.dumps(cfg, indent=2))


def http_ok(url: str, headers: dict = None, timeout: float = 5) -> bool:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        return urllib.request.urlopen(req, timeout=timeout).status == 200
    except Exception:
        return False


def gh(*args, input_text: str = None) -> str:
    return subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True, check=True).stdout


def write_switch(enabled: bool, url: str = "") -> None:
    cfg = load_cfg()
    if not cfg.get("gist_id"):
        sys.exit("No switch yet. Run: python scripts/relay.py setup")
    content = json.dumps({"enabled": enabled, "url": url,
                          "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2)
    gh("api", "-X", "PATCH", f"gists/{cfg['gist_id']}", "--input", "-",
       input_text=json.dumps({"files": {GIST_FILE: {"content": content}}}))


def read_switch() -> dict:
    cfg = load_cfg()
    if not cfg.get("gist_id"):
        return {}
    files = json.loads(gh("api", f"gists/{cfg['gist_id']}"))["files"]
    return json.loads(files[GIST_FILE]["content"])


def start(name: str, cmd: list[str], env: dict) -> int:
    STATE.mkdir(exist_ok=True)
    log = open(STATE / f"{name}.log", "w")
    p = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)       # survives this script and the terminal
    (STATE / f"{name}.pid").write_text(str(p.pid))
    return p.pid


def stop(name: str) -> None:
    pid_file = STATE / f"{name}.pid"
    if not pid_file.exists():
        return
    try:
        os.killpg(int(pid_file.read_text()), signal.SIGTERM)
    except (ProcessLookupError, ValueError, PermissionError):
        pass
    pid_file.unlink()


# ── commands ────────────────────────────────────────────────────────────────
def setup() -> None:
    cfg = load_cfg()
    if not cfg.get("gist_id"):
        tmp = STATE / GIST_FILE
        STATE.mkdir(exist_ok=True)
        tmp.write_text(json.dumps({"enabled": False, "url": ""}, indent=2))
        url = gh("gist", "create", "--desc", "swellapi relay switch", str(tmp)).strip().splitlines()[-1]
        gist_id = url.rstrip("/").split("/")[-1]
        owner = json.loads(gh("api", "user"))["login"]
        cfg = {"gist_id": gist_id, "gist_url": url,
               "raw_url": f"https://gist.githubusercontent.com/{owner}/{gist_id}/raw/{GIST_FILE}"}
        save_cfg(cfg)
        tmp.unlink()
    load_env()
    print("Switch (edit to toggle):", cfg["gist_url"])
    print()
    print("Set these once on Render (swellapi-1 > Environment):")
    print("  RELAY_CONFIG_URL =", cfg["raw_url"])
    print(f"  PROXY_TOKEN      = (the value in {ENV_FILE.name})")
    print("and remove OPEN_METEO_PROXY if it is set.")


def on() -> None:
    env = load_env()
    port = env.get("PORT", "8001")
    local = f"http://localhost:{port}"

    if not http_ok(f"{local}/health"):
        start("relay", ["caffeinate", "-i", sys.executable, "-W", "ignore", "api_prox.py"], env)
        for _ in range(40):
            if http_ok(f"{local}/health"):
                break
            time.sleep(0.5)
        else:
            sys.exit(f"relay did not start; see {STATE / 'relay.log'}")
    print("relay   ", local)

    stop("tunnel")
    start("tunnel", ["cloudflared", "tunnel", "--no-autoupdate", "--url", local], {})
    url = None
    for _ in range(60):
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", (STATE / "tunnel.log").read_text())
        if m:
            url = m.group(0)
            break
        time.sleep(0.5)
    if not url:
        sys.exit(f"tunnel did not start; see {STATE / 'tunnel.log'}")
    for _ in range(30):                       # new hostnames take a few seconds to resolve
        if http_ok(f"{url}/health"):
            break
        time.sleep(2)
    else:
        sys.exit(f"tunnel {url} not reachable yet; try `status` in a minute")
    print("tunnel  ", url)

    write_switch(True, url)
    print("switch   ON (Render picks it up within ~5 min: GitHub caches raw gists)")


def off() -> None:
    try:
        write_switch(False)
        print("switch   OFF")
    except SystemExit:
        raise
    except Exception as e:
        print("could not update switch:", e)
    stop("tunnel")
    stop("relay")
    print("relay and tunnel stopped")


def status() -> None:
    env = load_env()
    local = f"http://localhost:{env.get('PORT', '8001')}"
    sw = read_switch()
    token = {"X-Proxy-Token": env.get("PROXY_TOKEN", "")}
    print("relay   ", "up" if http_ok(f"{local}/health") else "down", local)
    if sw.get("url"):
        print("tunnel  ", "up" if http_ok(f"{sw['url']}/health") else "down", sw["url"])
        probe = f"{sw['url']}/forecast?latitude=32.3&longitude=-64.8&hourly=wind_speed_10m&forecast_days=1"
        print("upstream", "ok" if http_ok(probe, token, timeout=20) else "failing")
    print("switch  ", "ON" if sw.get("enabled") else "OFF", f"(updated {sw.get('updated', '?')})" if sw else "(not set up)")
    if load_cfg().get("gist_url"):
        print("edit    ", load_cfg()["gist_url"])


if __name__ == "__main__":
    cmds = {"setup": setup, "on": on, "off": off, "status": status}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]]()
