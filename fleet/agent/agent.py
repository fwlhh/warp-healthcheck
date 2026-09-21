#!/usr/bin/env python3
"""warp-agent — heartbeat + command executor for one WARP node."""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        print(f"FATAL: {key} is not set", file=sys.stderr)
        sys.exit(1)
    return val or ""


COORDINATOR_URL = env("COORDINATOR_URL", required=True).rstrip("/")
NODE_NAME       = env("NODE_NAME", required=True)
NODE_TOKEN      = env("NODE_TOKEN", required=True)

WARP_PROXY      = env("WARP_PROXY", "127.0.0.1:1080")
COMPOSE_DIR     = env("COMPOSE_DIR", "/opt/warp")
COMPOSE_FILE    = env("COMPOSE_FILE", "docker-compose.yml")
COMPOSE_SERVICE = env("COMPOSE_SERVICE", "warp")

HEARTBEAT_INTERVAL = int(env("HEARTBEAT_INTERVAL", "60"))
POLL_INTERVAL      = int(env("POLL_INTERVAL", "5"))
CHECK_INTERVAL     = int(env("CHECK_INTERVAL", "300"))
RESTART_COOLDOWN   = int(env("RESTART_COOLDOWN", "300"))
CURL_TIMEOUT       = int(env("CURL_TIMEOUT", "10"))

STATE_DIR  = env("STATE_DIR", "/var/lib/warp-agent")
STATE_FILE = os.path.join(STATE_DIR, "last_restart")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)


def read_last_restart() -> int:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def write_last_restart(ts: int) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(ts))
    except Exception as e:
        print(f"state write failed: {e}", flush=True)


def curl(opts: list, url: str) -> str:
    cmd = ["curl", *opts, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=CURL_TIMEOUT + 10)
        return r.stdout
    except Exception:
        return ""


def warp_alive() -> bool:
    out = curl(
        ["-fs", "--max-time", "8", "--socks5-hostname", WARP_PROXY],
        "https://cloudflare.com/cdn-cgi/trace",
    )
    return bool(out.strip())


def detect_google_country() -> str:
    opts = [
        "-fsL",
        "--max-time", str(CURL_TIMEOUT),
        "-A", USER_AGENT,
        "--socks5-hostname", WARP_PROXY,
    ]
    resp = curl(opts, "https://www.google.com")
    m = re.search(r'"[a-z]{2}_([A-Z]{2})"', resp)
    if m:
        return m.group(1)
    resp = curl(opts, "https://play.google.com/")
    m = re.search(r'"countryCode":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)
    return ""


def do_restart() -> tuple[bool, str]:
    cmd = ["docker", "compose", "-f", os.path.join(COMPOSE_DIR, COMPOSE_FILE), "restart"]
    if COMPOSE_SERVICE:
        cmd.append(COMPOSE_SERVICE)
    print(f"running: {' '.join(cmd)}", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _headers() -> dict:
    return {
        "X-Node": NODE_NAME,
        "X-Token": NODE_TOKEN,
        "Content-Type": "application/json",
    }


def coordinator_post(path: str, body: dict) -> dict | None:
    req = urllib.request.Request(
        f"{COORDINATOR_URL}{path}",
        data=json.dumps(body).encode(),
        headers=_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"POST {path} http {e.code}", flush=True)
    except Exception as e:
        print(f"POST {path} failed: {e}", flush=True)
    return None


def coordinator_get(path: str) -> dict | None:
    req = urllib.request.Request(f"{COORDINATOR_URL}{path}", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"GET {path} http {e.code}", flush=True)
    except Exception as e:
        print(f"GET {path} failed: {e}", flush=True)
    return None


def handle_command(c: dict) -> None:
    cid = c.get("id")
    cmd = c.get("cmd")
    if cmd == "restart":
        ok, out = do_restart()
        if ok:
            write_last_restart(int(time.time()))
        coordinator_post("/result", {"id": cid, "ok": ok, "output": out[-4000:]})
    else:
        coordinator_post(
            "/result",
            {"id": cid, "ok": False, "output": f"unknown command: {cmd}"},
        )


def main() -> int:
    print(f"agent started node={NODE_NAME} coordinator={COORDINATOR_URL}", flush=True)

    last_check = 0
    last_heartbeat = 0
    last_restart = read_last_restart()
    google_country = ""
    alive = False

    while True:
        t = time.time()

        if t - last_check >= CHECK_INTERVAL:
            last_check = t
            if warp_alive():
                alive = True
                google_country = detect_google_country()
                print(f"check: google={google_country or '?'}", flush=True)
                if google_country == "RU" and t - last_restart >= RESTART_COOLDOWN:
                    ok, out = do_restart()
                    if ok:
                        last_restart = int(t)
                        write_last_restart(last_restart)
                        coordinator_post(
                            "/notify",
                            {"text": "🇷🇺 Google=RU detected, WARP restarted"},
                        )
                        print("auto-restart ok", flush=True)
                    else:
                        coordinator_post(
                            "/notify",
                            {"text": f"❌ auto-restart failed: {out[:500]}"},
                        )
                        print(f"auto-restart failed: {out}", flush=True)
            else:
                alive = False
                google_country = ""
                print("warp unreachable", flush=True)

        if t - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = t
            coordinator_post(
                "/heartbeat",
                {
                    "google_country": google_country or None,
                    "warp_alive": alive,
                    "last_restart": last_restart,
                },
            )

        resp = coordinator_get("/commands")
        if resp and isinstance(resp.get("commands"), list):
            for c in resp["commands"]:
                try:
                    handle_command(c)
                except Exception as e:
                    print(f"handle_command failed: {e}", flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())