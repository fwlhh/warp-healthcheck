#!/usr/bin/env python3
"""warp-agent — heartbeat + command executor for one WARP node.

Design notes:
  * Every HTTP call to the coordinator has a timeout and is retried on
    the next loop iteration.
  * Results that fail to POST are queued to disk and re-sent later,
    together with the structured post-restart report.
  * Every subprocess call has a timeout.
  * Every state transition is logged.
"""

import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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

# The first WARP of the node. Further ones are WARP2_*, WARP3_*, ... (see
# load_warps); there is no limit on their number.
WARP_PROXY = env("WARP_PROXY", "127.0.0.1:1080")
COMPOSE_DIR = env("COMPOSE_DIR", "/opt/warp")
COMPOSE_FILE = env("COMPOSE_FILE", "docker-compose.yml")
COMPOSE_SERVICE = env("COMPOSE_SERVICE", "warp")
WARP_NAME = env("WARP_NAME", "")

HEARTBEAT_INTERVAL = int(env("HEARTBEAT_INTERVAL", "60"))
POLL_INTERVAL = int(env("POLL_INTERVAL", "5"))
CHECK_INTERVAL = int(env("CHECK_INTERVAL", "300"))
RESTART_COOLDOWN = int(env("RESTART_COOLDOWN", "60"))
CURL_TIMEOUT = int(env("CURL_TIMEOUT", "30"))
HTTP_TIMEOUT = int(env("HTTP_TIMEOUT", "20"))
WARP_BOOT_TIMEOUT = int(env("WARP_BOOT_TIMEOUT", "60"))
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
# Countries in which Google refuses the service (Gemini and others): seeing one
# of them as the exit's country triggers a restart. Space or comma separated.
BAD_COUNTRIES = frozenset(env("BAD_COUNTRIES", "RU").replace(",", " ").upper().split())

STATE_DIR: Path = Path(env("STATE_DIR", "/var/lib/warp-agent"))
STATE_FILE: Path = STATE_DIR / "state.json"
PENDING_DIR: Path = STATE_DIR / "pending"

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


LOG = logging.getLogger("warp-agent")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


def log(level: str, msg: str) -> None:
    LOG.log(getattr(logging, level.upper(), logging.INFO), msg)


# ===========================================================================
# WARP instances
# ===========================================================================

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_WARPN_RE = re.compile(r"^WARP(\d+)_(PROXY|SERVICE|NAME|COMPOSE_DIR|COMPOSE_FILE)$")


class ConfigError(Exception):
    pass


class Warp:
    """One WARP container of the node: where its SOCKS5 listens and which
    compose service restarts it."""

    def __init__(self, name, proxy, service, compose_dir, compose_file):
        self.name = name
        self.proxy = proxy
        self.service = service
        self.compose_dir = compose_dir
        self.compose_file = compose_file

    def __repr__(self):
        return f"Warp({self.name!r}, proxy={self.proxy!r}, service={self.service!r})"


def normalize_proxy(value):
    """'127.0.0.1:1081' as is; docker's 'host:published:container' form
    ('127.0.0.1:1081:1080') is reduced to 'host:published'."""
    value = (value or "").strip()
    if value.startswith("["):  # [::1]:1080
        return value
    parts = value.split(":")
    if len(parts) == 3:
        return f"{parts[0]}:{parts[1]}"
    return value


def load_warps(environ=None):
    """Build the list of WARPs from the environment.

    WARP #1 comes from the long-standing WARP_PROXY / COMPOSE_SERVICE
    variables (WARP1_* may override them), so existing installs keep working.
    Every further WARP is a group of variables with its number:

        WARP2_NAME=warp2              (optional, defaults to the service name)
        WARP2_PROXY=127.0.0.1:1081
        WARP2_SERVICE=warp2           (the docker compose service to restart)
        WARP2_COMPOSE_DIR / WARP2_COMPOSE_FILE   (optional)

    Any number of groups is accepted; they are discovered, not counted.
    """
    environ = os.environ if environ is None else environ
    numbers = {1}
    for key in environ:
        m = _WARPN_RE.match(key)
        if m and environ[key]:
            numbers.add(int(m.group(1)))

    warps = []
    for n in sorted(numbers):
        def get(suffix, legacy=""):
            return environ.get(f"WARP{n}_{suffix}") or (legacy if n == 1 else "")

        proxy = get("PROXY", WARP_PROXY)
        service = get("SERVICE", COMPOSE_SERVICE)
        if not proxy:
            raise ConfigError(f"WARP{n}_PROXY is not set (WARP{n}_* variables exist)")
        if not service:
            raise ConfigError(f"WARP{n}_SERVICE is not set: nothing to restart for WARP #{n}")
        name = get("NAME", WARP_NAME) or service or f"warp{n}"
        if not _NAME_RE.match(name):
            raise ConfigError(f"WARP #{n}: name {name!r} must match [A-Za-z0-9._-]{{1,32}}")
        warps.append(
            Warp(
                name,
                normalize_proxy(proxy),
                service,
                get("COMPOSE_DIR", COMPOSE_DIR) or COMPOSE_DIR,
                get("COMPOSE_FILE", COMPOSE_FILE) or COMPOSE_FILE,
            )
        )

    for attr in ("name", "proxy", "service"):
        seen = {}
        for w in warps:
            value = getattr(w, attr)
            if value in seen:
                raise ConfigError(f"two WARPs share the same {attr} {value!r}")
            seen[value] = w
    return warps


def _configured_warps():
    try:
        return load_warps()
    except ConfigError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)


WARPS = _configured_warps()


def find_warp(name):
    """The WARP called `name`, or None. An empty name is the first WARP."""
    if not name:
        return WARPS[0]
    return next((w for w in WARPS if w.name == name), None)


# ===========================================================================
# State
# ===========================================================================


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_restart": 0}


def _save_state(s: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(STATE_FILE)
    except Exception as e:
        LOG.error(f"state save: {e}")


def get_last_restart(name=None):
    """Unix time of the last successful restart of one WARP (the first by
    default). Reads the legacy single "last_restart" key for the first WARP,
    so a state file written before several WARPs were supported still counts."""
    name = name or WARPS[0].name
    s = _load_state()
    entry = (s.get("warps") or {}).get(name)
    if isinstance(entry, dict) and "last_restart" in entry:
        return int(entry["last_restart"] or 0)
    if name == WARPS[0].name:
        return int(s.get("last_restart", 0) or 0)
    return 0


# What the heartbeat reports. This is shared state, not locals of main(), so
# that a restart (manual or automatic) refreshes it. As locals they were only
# updated by the periodic check every CHECK_INTERVAL, and the next heartbeat
# repeated the pre-restart country, overwriting the fresh one the coordinator
# had just received with the restart result ("RU" again for minutes).
STATE = {"warps": {}, "heartbeat_now": False}


def warp_state(name=None):
    """What the heartbeat reports about one WARP (the first by default)."""
    return STATE["warps"].setdefault(
        name or WARPS[0].name, {"alive": False, "google_country": ""}
    )


def remember_restart(report, name=None):
    """Adopt the result of a restart and send a heartbeat right away."""
    up = bool(report.get("warp_up"))
    country = report.get("country") or ""
    st = warp_state(name)
    st["alive"] = up
    st["google_country"] = country if up and country != "?" else ""
    STATE["heartbeat_now"] = True


def cooldown_remaining(now=None, name=None) -> int:
    """Seconds left before another restart of this WARP is allowed (0 = now).

    Always reads the persisted timestamp, so manual and automatic restarts
    share one cooldown per WARP, matching the single-node bot.
    """
    now = time.time() if now is None else now
    return max(0, int(RESTART_COOLDOWN - (now - get_last_restart(name))))


def set_last_restart(ts, name=None):
    name = name or WARPS[0].name
    s = _load_state()
    s.setdefault("warps", {}).setdefault(name, {})["last_restart"] = int(ts)
    if name == WARPS[0].name:
        s["last_restart"] = int(ts)  # keep the legacy key readable
    _save_state(s)


# ===========================================================================
# Pending results queue
# ===========================================================================


def _pending_path(cid: int) -> Path:
    return PENDING_DIR / f"{cid}.json"


def queue_result(cid, ok, output, meta=None):
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _pending_path(cid).with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "id": cid,
                    "ok": ok,
                    "output": output,
                    "meta": meta or {},
                    "queued_at": int(time.time()),
                }
            )
        )
        tmp.replace(_pending_path(cid))
        log("WARN", f"queued result cmd#{cid} for later delivery")
    except Exception as e:
        LOG.error(f"queue_result: {e}")


def _pending_files() -> list[Path]:
    try:
        return sorted(PENDING_DIR.iterdir())
    except FileNotFoundError:
        return []


def flush_pending():
    """Try to deliver queued results. Called before polling commands."""
    files = _pending_files()
    if not files:
        return
    for path in files:
        try:
            payload = json.loads(path.read_text())
        except Exception as e:
            log("ERROR", f"pending read {path.name}: {e}")
            with contextlib.suppress(Exception):
                path.unlink()
            continue

        ok, _ = http_post("/result", payload)
        if ok:
            log("INFO", f"flushed pending result cmd#{payload.get('id')}")
            with contextlib.suppress(Exception):
                path.unlink()


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


# Every probe takes the WARP to ask through; without one, the first WARP.


def warp_alive(w=None):
    w = w or WARPS[0]
    out = _curl(
        ["-fs", "--max-time", "8", "--socks5-hostname", w.proxy],
        "https://cloudflare.com/cdn-cgi/trace",
    )
    return bool(out.strip())


def warp_trace(w=None):
    w = w or WARPS[0]
    return _curl(
        ["-s", "--max-time", "8", "--socks5-hostname", w.proxy],
        "https://cloudflare.com/cdn-cgi/trace",
    )


def warp_exit_ip(w=None):
    m = re.search(r"^ip=(\S+)", warp_trace(w), re.MULTILINE)
    return m.group(1) if m else ""


def warp_exit_country(w=None):
    m = re.search(r"^loc=([A-Z]{2})", warp_trace(w), re.MULTILINE)
    return m.group(1) if m else ""


def _google_get(url, w=None):
    w = w or WARPS[0]
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
            w.proxy,
        ],
        url,
    )


def detect_google_country(w=None):
    resp = _google_get("https://www.youtube.com", w)
    m = re.search(r'"countryCode":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    resp = _google_get("https://www.google.com/search?q=test", w)
    m = re.search(r'"gl":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    resp = _google_get("https://accounts.google.com/", w)
    m = re.search(r'"countryCode":"([A-Z]{2})"', resp)
    if m:
        return m.group(1)

    return ""


# ===========================================================================
# Restart
# ===========================================================================


def compose_restart(w=None):
    w = w or WARPS[0]
    cmd = [
        "docker",
        "compose",
        "-f",
        str(Path(w.compose_dir) / w.compose_file),
        "restart",
        w.service,
    ]
    log("INFO", f"[{w.name}] running: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        ok = r.returncode == 0
        out = (r.stdout + r.stderr).strip()
        return ok, out
    except subprocess.TimeoutExpired:
        return False, "docker compose restart timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def wait_warp_up(w=None):
    deadline = time.time() + WARP_BOOT_TIMEOUT
    while time.time() < deadline:
        if warp_alive(w):
            return True
        time.sleep(3)
    return False


def collect_post_restart_report(w=None, previous_ip="") -> dict:
    """Probe WARP and Google after a restart. Returns a structured dict.

    `still_ru` means "still in a country of BAD_COUNTRIES" (the name is kept
    for coordinators that already know it).
    """
    w = w or WARPS[0]
    report = {
        "warp": w.name,
        "warp_up": False,
        "country": "",
        "cloudflare_loc": "",
        "exit_ip": "",
        "previous_ip": previous_ip,
        "still_ru": False,
    }
    if not wait_warp_up(w):
        return report
    report["warp_up"] = True
    report["country"] = detect_google_country(w)
    report["cloudflare_loc"] = warp_exit_country(w)
    report["exit_ip"] = warp_exit_ip(w)
    report["still_ru"] = report["country"] in BAD_COUNTRIES
    return report


def format_post_restart_report(report: dict) -> str:
    """Human-readable fallback for logs and legacy paths."""
    if not report.get("warp_up"):
        return f"WARP did not come up within {WARP_BOOT_TIMEOUT}s"
    country = report.get("country") or "?"
    lines = [f"Google now: {country}"]
    cc = report.get("cloudflare_loc")
    if cc:
        lines.append(f"Cloudflare loc: {cc}")
    ip, before = report.get("exit_ip"), report.get("previous_ip")
    if ip and before and ip != before:
        lines.append(f"Exit IP: {before} -> {ip}")
    elif ip:
        lines.append(f"Exit IP: {ip}" + (" (unchanged)" if ip == before else ""))
    if report.get("still_ru"):
        lines.append(f"still {country}")
    return "\n".join(lines)


def do_restart_and_report(w=None):
    """Restart one WARP. Returns (ok, text_summary, structured_report)."""
    w = w or WARPS[0]
    # The exit address before the restart: after it, "old -> new" tells whether
    # the restart actually moved the exit (an unchanged address helps nobody).
    previous_ip = warp_exit_ip(w)
    log("INFO", f"[{w.name}] restart: running docker compose restart")
    ok, out = compose_restart(w)
    if not ok:
        log("ERROR", f"[{w.name}] restart: compose failed: {out}")
        return False, f"compose restart failed:\n{out}", {"warp": w.name}

    log("INFO", f"[{w.name}] restart: compose ok, waiting for WARP")
    report = collect_post_restart_report(w, previous_ip)
    text = format_post_restart_report(report)
    full = f"{out}\n\n--- after restart ---\n{text}" if out else text
    return True, full, report


# ===========================================================================
# Command handling
# ===========================================================================


def restart_one(w):
    """Restart one WARP unless its cooldown is active.

    Returns (ok, output, report).
    """
    remaining = cooldown_remaining(name=w.name)
    if remaining > 0:
        out = f"[{w.name}] restart skipped: cooldown active ({remaining}s remaining)"
        log("INFO", out)
        # Say what happened. An empty report would be read by the coordinator
        # as "WARP did not come up".
        return (
            False,
            out,
            {"warp": w.name, "skipped": "cooldown", "cooldown_remaining": remaining},
        )
    ok, out, report = do_restart_and_report(w)
    if ok:
        set_last_restart(int(time.time()), w.name)
        remember_restart(report, w.name)
    return ok, out, report


def handle_command(c):
    cid = c.get("id")
    name = c.get("cmd")
    target = c.get("warp") or ""
    log("INFO", f"received cmd#{cid}: {name}" + (f" warp={target}" if target else ""))

    if name != "restart":
        queue_result(cid, False, f"unknown command: {name}")
        return

    if target:
        w = find_warp(target)
        if w is None:
            known = ", ".join(x.name for x in WARPS)
            ok, out, report = False, f"unknown warp {target!r} (this node has: {known})", {}
        else:
            ok, out, report = restart_one(w)
    elif len(WARPS) == 1:
        ok, out, report = restart_one(WARPS[0])
    else:
        # No target on a node with several WARPs: restart them one after
        # another and report them together (the coordinator normally names the
        # WARP, so this is the fallback for a bare "restart").
        results = [restart_one(w) for w in WARPS]
        ok = all(r[0] for r in results)
        out = "\n\n".join(r[1] for r in results)
        report = results[-1][2]

    payload = {"id": cid, "ok": ok, "output": out[-4000:], "meta": report}
    sent, _ = http_post("/result", payload)
    if not sent:
        queue_result(cid, ok, out[-4000:], meta=report)
    else:
        log("INFO", f"reported cmd#{cid}: ok={ok}")


# ===========================================================================
# Main loop
# ===========================================================================


def heartbeat_payload():
    """The state of every WARP, plus the single-WARP fields that coordinators
    without multi-WARP support read."""
    now = time.time()
    warps = []
    for w in WARPS:
        st = warp_state(w.name)
        warps.append(
            {
                "name": w.name,
                "google_country": st["google_country"] or None,
                "alive": bool(st["alive"]),
                "last_restart": get_last_restart(w.name),
            }
        )
    bad = next((x for x in warps if x["google_country"] in BAD_COUNTRIES), None)
    headline = bad or warps[0]
    return {
        "google_country": headline["google_country"],
        "warp_alive": all(x["alive"] for x in warps),
        "last_restart": max(x["last_restart"] for x in warps),
        "warps": warps,
        "sent_at": int(now),
    }


def check_warp(w, t):
    """Periodic check of one WARP: probe it and restart it if Google sees a
    BAD_COUNTRIES country and the cooldown allows."""
    st = warp_state(w.name)
    if not warp_alive(w):
        st["alive"] = False
        st["google_country"] = ""
        log("WARN", f"[{w.name}] warp unreachable")
        return
    st["alive"] = True
    st["google_country"] = detect_google_country(w)
    log("INFO", f"[{w.name}] check: google={st['google_country'] or '?'}")
    if st["google_country"] not in BAD_COUNTRIES:
        return
    remaining = cooldown_remaining(t, w.name)
    if remaining > 0:
        log("INFO", f"[{w.name}] auto: Google={st['google_country']}, cooldown {remaining}s")
        return
    country = st["google_country"]
    log("WARN", f"[{w.name}] auto: Google={country}, restarting")
    ok, out, report = do_restart_and_report(w)
    if ok:
        set_last_restart(int(t), w.name)
        remember_restart(report, w.name)
        http_post(
            "/notify",
            {
                "text": (
                    f"Google={country} detected on {w.name}, WARP restarted\n"
                    + format_post_restart_report(report)
                ),
                "reason": "auto",
                "warp": w.name,
                "meta": report,
            },
        )
        log("INFO", f"[{w.name}] auto-restart ok")
    else:
        http_post(
            "/notify",
            {
                "text": f"auto-restart of {w.name} failed: {out[:500]}",
                "reason": "auto-fail",
                "warp": w.name,
            },
        )
        log("ERROR", f"[{w.name}] auto-restart failed: {out}")


def main():
    setup_logging(LOG_LEVEL)
    log("INFO", f"agent started node={NODE_NAME} coordinator={COORDINATOR_URL}")
    for w in WARPS:
        log("INFO", f"warp {w.name}: proxy={w.proxy} service={w.service}")
    if BAD_COUNTRIES:
        log("INFO", f"restart when Google sees: {' '.join(sorted(BAD_COUNTRIES))}")

    last_check = 0
    last_heartbeat = 0

    while True:
        t = time.time()

        # 1. Periodic Google check + auto-restart, one WARP after another.
        if t - last_check >= CHECK_INTERVAL:
            last_check = t
            for w in WARPS:
                check_warp(w, t)

        # 2. Heartbeat: on schedule, and right after a restart.
        if STATE["heartbeat_now"] or t - last_heartbeat >= HEARTBEAT_INTERVAL:
            last_heartbeat = t
            STATE["heartbeat_now"] = False
            http_post("/heartbeat", heartbeat_payload())

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


def cli_status():
    """`warp-agent status`: probe every WARP once and print a table."""
    now = time.time()
    print(f"node {NODE_NAME}; restart when Google sees: {' '.join(sorted(BAD_COUNTRIES)) or '-'}")
    for w in WARPS:
        alive = warp_alive(w)
        country = detect_google_country(w) if alive else ""
        ip = warp_exit_ip(w) if alive else ""
        wait = cooldown_remaining(now, w.name)
        print(
            f"  {w.name:<16} proxy={w.proxy:<22} service={w.service:<14} "
            f"{'up  ' if alive else 'DOWN'} google={country or '?':<3} ip={ip or '-'} "
            f"cooldown={wait}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(cli_status() if sys.argv[1:] == ["status"] else main())
