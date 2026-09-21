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
WARP_BOOT_TIMEOUT  = int(env("WARP_BOOT_TIMEOUT", "45"))

STATE_DIR  = env("STATE_DIR", "/var/lib/warp-agent")
STATE_FILE = os.path.join(STATE_DIR, "last_restart")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
GOOGLE_CONSENT_COOKIE = (
    "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjUwNzMw"
    "LjA1X3AwGgJlbiACGgYIgPC_xAY"
)


# ---------------------------------------------------------------------------
# Local state
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# WARP helpers
# ---------------------------------------------------------------------------

def curl(opts: list, url: str, timeout: int | None = None) -> str:
    cmd = ["curl", *opts, url]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=(timeout or CURL_TIMEOUT + 10),
        )
        return r.stdout
    except Exception:
        return ""


def warp_alive() -> bool:
    out = curl(
        ["-fs", "--max-time", "8", "--socks5-hostname", WARP_PROXY],
        "https://cloudflare.com/cdn-cgi/trace",
    )
    return bool(out.strip())


def warp_exit_ip() -> str:
    trace = curl(
        ["-s", "--max-time", "8", "--socks5-hostname", WARP_PROXY],
        "https://cloudflare.com/cdn-cgi/trace",
    )
    m = re.search(r"^ip=(\S+)", trace, re.MULTILINE)
    return m.group(1) if m else ""


def warp_exit_country() -> str:
    trace = curl(
        ["-s", "--max-time", "8", "--socks5-hostname", WARP_PROXY],
        "https://cloudflare.com/cdn-cgi/trace",
    )
    m = re.search(r"^loc=([A-Z]{2})", trace, re.MULTILINE)
    return m.group(1) if m else ""


def _curl_google(url: str) -> str:
    return curl(
        [
            "-fsSL",
            "--max-time", str(CURL_TIMEOUT),
            "-A", USER_AGENT,
            "-H", f"Cookie: {GOOGLE_CONSENT_COOKIE}",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "--socks5-hostname", WARP_PROXY,
        ],
        url,
    )


def detect_google_country() -> str:
    # 1) YouTube
    resp = _curl_google("https://www.youtube.com")
    m = re.search(r'"countryCode":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    # 2) Google Search
    resp = _curl_google("https://www.google.com/search?q=test")
    m = re.search(r'"gl":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    # 3) accounts.google.com
    resp = _curl_google("https://accounts.google.com/")
    m = re.search(r'"countryCode":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    return ""


def wait_for_warp() -> bool:
    waited = 0
    while waited < WARP_BOOT_TIMEOUT:
        if warp_alive():
            return True
        time.sleep(3)
        waited += 3
    return False


def post_restart_report() -> str:
    """Returns a short human-readable report after a restart."""
    if not wait_for_warp():
        return f"⚠️ WARP did not come up within {WARP_BOOT_TIMEOUT}s"

    country = detect_google_country()
    cloudflare_country = warp_exit_country()
    ip = warp_exit_ip()

    lines = [f"Google now: {country or '?'}"]
    if cloudflare_country:
        lines.append(f"Cloudflare loc: {cloudflare_country}")
    if ip:
        lines.append(f"Exit IP: {ip}")
    if country == "RU":
        lines.append("🔴 still RU — try /restart again after cooldown")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Coordinator HTTP
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "X-Node": NODE_NAME,
        "X-Token": NODE_TOKEN,
        "Content-Type": "application/json",
    }


def _log_http_error(verb: str, path: str, e: urllib.error.HTTPError) -> None:
    if e.code == 401:
        print(
            f"{verb} {path}: 401 unauthorized — "
            f"check NODE_NAME/NODE_TOKEN against coordinator",
            flush=True,
        )
    else:
        print(f"{verb} {path} http {e.code}", flush=True)


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
        _log_http_error("POST", path, e)
    except Exception as e:
        print(f"POST {path} failed: {e}", flush=True)
    return None


def coordinator_get(path: str) -> dict | None:
    req = urllib.request.Request(f"{COORDINATOR_URL}{path}", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        _log_http_error("GET", path, e)
    except Exception as e:
        print(f"GET {path} failed: {e}", flush=True)
    return None


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------

def handle_command(c: dict) -> None:
    cid = c.get("id")
    cmd = c.get("cmd")

    if cmd != "restart":
        coordinator_post(
            "/result",
            {"id": cid, "ok": False, "output": f"unknown command: {cmd}"},
        )
        return

    ok, out = do_restart()
    if ok:
        write_last_restart(int(time.time()))
        report = post_restart_report()
        out = f"{out}\n\n--- after restart ---\n{report}" if out else report

    coordinator_post("/result", {"id": cid, "ok": ok, "output": out[-4000:]})


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"agent started node={NODE_NAME} coordinator={COORDINATOR_URL}", flush=True)

    last_check = 0
    last_heartbeat = 0
    last_restart = read_last_restart()
    google_country = ""
    alive = False

    while True:
        t = time.time()

        # 1) Periodic Google country probe, auto-restart on RU.
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
                        report = post_restart_report()
                        coordinator_post(
                            "/notify",
                            {
                                "text": (
                                    "🇷🇺 Google=RU detected, WARP restarted\n"
                                    f"{report}"
                                )
                            },
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

        # 2) Heartbeat.
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

        # 3) Pull and execute commands.
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