#!/usr/bin/env python3
"""warp-agent — heartbeat + command executor for one WARP node.

Design notes:
  * Every HTTP call to the coordinator has a timeout and is retried on
    the next loop iteration.
  * Results that fail to POST are queued to disk and re-sent later.
  * Every subprocess call has a timeout.
  * Every state transition is logged.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ===========================================================================
# Config
# ===========================================================================


def env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        print(f"FATAL: {key} is not set", file=sys.stderr)
        sys.exit(1)
    return val or ""


COORDINATOR_URL = env("COORDINATOR_URL", required=True).rstrip("/")
NODE_NAME = env("NODE_NAME", required=True)
NODE_TOKEN = env("NODE_TOKEN", required=True)

WARP_PROXY = env("WARP_PROXY", "127.0.0.1:1080")
COMPOSE_DIR = env("COMPOSE_DIR", "/opt/warp")
COMPOSE_FILE = env("COMPOSE_FILE", "docker-compose.yml")
COMPOSE_SERVICE = env("COMPOSE_SERVICE", "warp")

HEARTBEAT_INTERVAL = int(env("HEARTBEAT_INTERVAL", "60"))
POLL_INTERVAL = int(env("POLL_INTERVAL", "5"))
CHECK_INTERVAL = int(env("CHECK_INTERVAL", "300"))
RESTART_COOLDOWN = int(env("RESTART_COOLDOWN", "300"))
CURL_TIMEOUT = int(env("CURL_TIMEOUT", "30"))
HTTP_TIMEOUT = int(env("HTTP_TIMEOUT", "20"))
WARP_BOOT_TIMEOUT = int(env("WARP_BOOT_TIMEOUT", "60"))
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()

STATE_DIR = env("STATE_DIR", "/var/lib/warp-agent")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PENDING_DIR = os.path.join(STATE_DIR, "pending")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
GOOGLE_CONSENT_COOKIE = (
    "SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjUwNzMw"
    "LjA1X3AwGgJlbiACGgYIgPC_xAY"
)


# ===========================================================================
# Logging
# ===========================================================================

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


def log(level, msg):
    if _LEVELS.get(level, 0) < _LEVELS.get(LOG_LEVEL, 20):
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} {level:5s} {msg}", flush=True)


# ===========================================================================
# State
# ===========================================================================


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_restart": 0}


def _save_state(s):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("ERROR", f"state save: {e}")


def get_last_restart():
    return int(_load_state().get("last_restart", 0) or 0)


def set_last_restart(ts):
    s = _load_state()
    s["last_restart"] = int(ts)
    _save_state(s)


# ===========================================================================
# Pending results queue
# ===========================================================================


def _pending_path(cid):
    return os.path.join(PENDING_DIR, f"{cid}.json")


def queue_result(cid, ok, output):
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        tmp = _pending_path(cid) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {"id": cid, "ok": ok, "output": output, "queued_at": int(time.time())},
                f,
            )
        os.replace(tmp, _pending_path(cid))
        log("WARN", f"queued result cmd#{cid} for later delivery")
    except Exception as e:
        log("ERROR", f"queue_result: {e}")


def _pending_files():
    try:
        return sorted(os.listdir(PENDING_DIR))
    except FileNotFoundError:
        return []


def flush_pending():
    """Try to deliver queued results. Called before polling commands."""
    files = _pending_files()
    if not files:
        return
    for fn in files:
        path = os.path.join(PENDING_DIR, fn)
        try:
            with open(path) as f:
                payload = json.load(f)
        except Exception as e:
            log("ERROR", f"pending read {fn}: {e}")
            with contextlib.suppress(Exception):
                os.remove(path)
            continue

        ok, _ = http_post("/result", payload)
        if ok:
            log("INFO", f"flushed pending result cmd#{payload.get('id')}")
            with contextlib.suppress(Exception):
                os.remove(path)


# ===========================================================================
# HTTP client
# ===========================================================================


def _headers():
    return {
        "X-Node": NODE_NAME,
        "X-Token": NODE_TOKEN,
        "Content-Type": "application/json",
    }


def _http(method, path, body=None):
    url = f"{COORDINATOR_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return True, json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            log(
                "ERROR",
                f"{method} {path}: 401 unauthorized (check NODE_NAME/NODE_TOKEN)",
            )
        else:
            log("WARN", f"{method} {path}: HTTP {e.code}")
        return False, None
    except Exception as e:
        log("WARN", f"{method} {path}: {type(e).__name__}: {e}")
        return False, None


def http_post(path, body):
    return _http("POST", path, body)


def http_get(path):
    return _http("GET", path)


# ===========================================================================
# WARP probes
# ===========================================================================


def _curl(opts, url):
    try:
        r = subprocess.run(
            ["curl", *opts, url],
            capture_output=True,
            text=True,
            timeout=CURL_TIMEOUT + 15,
        )
        return r.stdout
    except subprocess.TimeoutExpired:
        log("WARN", f"curl timeout for {url}")
        return ""
    except Exception as e:
        log("WARN", f"curl failed: {e}")
        return ""


def warp_alive():
    out = _curl(
        ["-fs", "--max-time", "8", "--socks5-hostname", WARP_PROXY],
        "https://cloudflare.com/cdn-cgi/trace",
    )
    return bool(out.strip())


def warp_trace():
    return _curl(
        ["-s", "--max-time", "8", "--socks5-hostname", WARP_PROXY],
        "https://cloudflare.com/cdn-cgi/trace",
    )


def warp_exit_ip():
    m = re.search(r"^ip=(\S+)", warp_trace(), re.MULTILINE)
    return m.group(1) if m else ""


def warp_exit_country():
    m = re.search(r"^loc=([A-Z]{2})", warp_trace(), re.MULTILINE)
    return m.group(1) if m else ""


def _google_get(url):
    return _curl(
        [
            "-fsSL",
            "--max-time",
            str(CURL_TIMEOUT),
            "-A",
            USER_AGENT,
            "-H",
            f"Cookie: {GOOGLE_CONSENT_COOKIE}",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "--socks5-hostname",
            WARP_PROXY,
        ],
        url,
    )


def detect_google_country():
    resp = _google_get("https://www.youtube.com")
    m = re.search(r'"countryCode":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    resp = _google_get("https://www.google.com/search?q=test")
    m = re.search(r'"gl":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    resp = _google_get("https://accounts.google.com/")
    m = re.search(r'"countryCode":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    return ""


# ===========================================================================
# Restart
# ===========================================================================


def compose_restart():
    cmd = [
        "docker",
        "compose",
        "-f",
        os.path.join(COMPOSE_DIR, COMPOSE_FILE),
        "restart",
    ]
    if COMPOSE_SERVICE:
        cmd.append(COMPOSE_SERVICE)
    log("INFO", f"running: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        ok = r.returncode == 0
        out = (r.stdout + r.stderr).strip()
        return ok, out
    except subprocess.TimeoutExpired:
        return False, "docker compose restart timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def wait_warp_up():
    deadline = time.time() + WARP_BOOT_TIMEOUT
    while time.time() < deadline:
        if warp_alive():
            return True
        time.sleep(3)
    return False


def post_restart_report():
    if not wait_warp_up():
        return f"⚠️ WARP did not come up within {WARP_BOOT_TIMEOUT}s"
    country = detect_google_country()
    cc = warp_exit_country()
    ip = warp_exit_ip()
    lines = [f"Google now: {country or '?'}"]
    if cc:
        lines.append(f"Cloudflare loc: {cc}")
    if ip:
        lines.append(f"Exit IP: {ip}")
    if country == "RU":
        lines.append("🔴 still RU")
    return "\n".join(lines)


def do_restart_and_report():
    log("INFO", "restart: running docker compose restart")
    ok, out = compose_restart()
    if not ok:
        log("ERROR", f"restart: compose failed: {out}")
        return False, f"compose restart failed:\n{out}"

    log("INFO", "restart: compose ok, waiting for WARP")
    report = post_restart_report()
    full = f"{out}\n\n--- after restart ---\n{report}" if out else report
    return True, full


# ===========================================================================
# Command handling
# ===========================================================================


def handle_command(c):
    cid = c.get("id")
    name = c.get("cmd")
    log("INFO", f"received cmd#{cid}: {name}")

    if name != "restart":
        queue_result(cid, False, f"unknown command: {name}")
        return

    ok, out = do_restart_and_report()
    if ok:
        set_last_restart(int(time.time()))

    sent, _ = http_post("/result", {"id": cid, "ok": ok, "output": out[-4000:]})
    if not sent:
        queue_result(cid, ok, out[-4000:])
    else:
        log("INFO", f"reported cmd#{cid}: ok={ok}")


# ===========================================================================
# Main loop
# ===========================================================================


def main():
    log("INFO", f"agent started node={NODE_NAME} coordinator={COORDINATOR_URL}")

    last_check = 0
    last_heartbeat = 0
    last_restart = get_last_restart()
    google_country = ""
    alive = False

    while True:
        t = time.time()

        # 1. Periodic Google check + auto-restart on RU.
        if t - last_check >= CHECK_INTERVAL:
            last_check = t
            if warp_alive():
                alive = True
                google_country = detect_google_country()
                log("INFO", f"check: google={google_country or '?'}")
                if google_country == "RU":
                    if t - last_restart >= RESTART_COOLDOWN:
                        log("WARN", "auto: Google=RU, restarting")
                        ok, out = do_restart_and_report()
                        if ok:
                            last_restart = int(t)
                            set_last_restart(last_restart)
                            http_post(
                                "/notify",
                                {
                                    "text": "🇷🇺 Google=RU detected, WARP restarted\n"
                                    + out
                                },
                            )
                            log("INFO", "auto-restart ok")
                        else:
                            http_post(
                                "/notify",
                                {"text": f"❌ auto-restart failed: {out[:500]}"},
                            )
                            log("ERROR", f"auto-restart failed: {out}")
                    else:
                        log("INFO", "auto: RU but cooldown active")
            else:
                alive = False
                google_country = ""
                log("WARN", "warp unreachable")

        # 2. Heartbeat.
        if t - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = t
            http_post(
                "/heartbeat",
                {
                    "google_country": google_country or None,
                    "warp_alive": alive,
                    "last_restart": last_restart,
                },
            )

        # 3. Flush any queued results from previous failures.
        flush_pending()

        # 4. Poll for commands.
        ok, resp = http_get("/commands")
        if ok and resp and isinstance(resp.get("commands"), list):
            for c in resp["commands"]:
                try:
                    handle_command(c)
                except Exception as e:
                    log("ERROR", f"handle_command: {type(e).__name__}: {e}")
                    queue_result(
                        c.get("id"), False, f"handler crashed: {type(e).__name__}: {e}"
                    )

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
