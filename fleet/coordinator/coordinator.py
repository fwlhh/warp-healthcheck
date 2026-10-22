#!/usr/bin/env python3
"""warp-coordinator — Telegram bot + HTTP API for warp-agents.

Design notes:
  * Bot long-polling runs in a daemon thread. If it dies, the process
    keeps serving HTTP but no commands arrive; the /health endpoint
    exposes bot_alive so external monitoring can catch this.
  * HTTP server runs in a daemon thread.
  * All external calls have explicit timeouts.
  * All state changes are logged.
  * Commands have a lease; undelivered results are re-queued after
    COMMAND_LEASE seconds, up to COMMAND_MAX_ATTEMPTS times.
  * After every restart the coordinator updates the node's stored
    google_country immediately, instead of waiting for the next
    heartbeat.
  * If a restart ends with an unknown country, the coordinator can queue
    another restart, at most RETRY_UNKNOWN_LIMIT times (0 disables it),
    each one delayed by RETRY_UNKNOWN_DELAY seconds so it respects the
    agent's RESTART_COOLDOWN. An unknown country usually means the probe
    failed, which a restart does not fix, so the default is conservative.
"""

import contextlib
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


HTTP_HOST = env("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(env("HTTP_PORT", "8080"))
DB_PATH = Path(env("DB_PATH", "/var/lib/warp-coordinator/coordinator.db"))
STALE_AFTER = int(env("STALE_AFTER", "180"))
# Proactive alerts when a node goes silent / loses WARP, and when it recovers.
NODE_ALERTS = env("NODE_ALERTS", "1").lower() not in ("0", "false", "no", "off", "")
WARP_DOWN_ALERT_AFTER = int(env("WARP_DOWN_ALERT_AFTER", "120"))
MONITOR_INTERVAL = int(env("MONITOR_INTERVAL", "30"))
# Dead-man's switch: ping this URL while the coordinator can still alert you.
DEADMAN_URL = env("DEADMAN_URL", "")
DEADMAN_INTERVAL = int(env("DEADMAN_INTERVAL", "60"))
COMMAND_LEASE = int(env("COMMAND_LEASE", "120"))
COMMAND_MAX_ATTEMPTS = int(env("COMMAND_MAX_ATTEMPTS", "3"))
RETRY_UNKNOWN_LIMIT = int(env("RETRY_UNKNOWN_LIMIT", "2"))
RETRY_UNKNOWN_DELAY = int(env("RETRY_UNKNOWN_DELAY", "330"))
# A command nobody fetched within COMMAND_TTL is stale intent: drop it rather
# than run it whenever the node next appears. Never shorter than a delayed
# retry plus a delivery window.
COMMAND_TTL = max(int(env("COMMAND_TTL", "600")), RETRY_UNKNOWN_DELAY + 120)
COMMAND_RETENTION_DAYS = int(env("COMMAND_RETENTION_DAYS", "30"))
POLL_TIMEOUT = int(env("POLL_TIMEOUT", "25"))
# Output the watchdog stores when it gives up on a command. A real result
# that arrives afterwards may still replace it once (see r_result).
LEASE_EXPIRED_OUTPUT = "lease expired after max attempts"
# HTTP limits: nodes send small JSON documents, so anything bigger is wrong,
# and a client that stalls must not hold a server thread forever.
MAX_BODY_BYTES = 64 * 1024
HTTP_SOCKET_TIMEOUT = int(env("HTTP_SOCKET_TIMEOUT", "30"))
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()

TG_TOKEN = ""
TG_CHAT_ID = 0


def load_tg_config():
    global TG_TOKEN, TG_CHAT_ID
    TG_TOKEN = env("TG_TOKEN", required=True)
    raw = env("TG_CHAT_ID", required=True)
    try:
        TG_CHAT_ID = int(raw)
    except ValueError:
        print(f"FATAL: TG_CHAT_ID must be an integer, got {raw!r}", file=sys.stderr)
        sys.exit(1)


# ===========================================================================
# Logging
# ===========================================================================


LOG = logging.getLogger("warp-coordinator")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


def log(level: str, msg: str) -> None:
    """Kept for compatibility; prefer LOG.info/warning/error directly."""
    LOG.log(getattr(logging, level.upper(), logging.INFO), msg)


# ===========================================================================
# Database
# ===========================================================================


def now() -> int:
    return int(time.time())


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _table_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _migrate_commands(conn):
    """Add columns introduced after the initial schema."""
    cols = _table_columns(conn, "commands")
    additions = [
        ("delivered_at", "INTEGER"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("ok", "INTEGER"),
        ("output", "TEXT"),
        ("result_at", "INTEGER"),
        ("not_before", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for name, typ in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE commands ADD COLUMN {name} {typ}")
            log("INFO", f"migration: added commands.{name}")


def _migrate_nodes(conn):
    cols = _table_columns(conn, "nodes")
    additions = [
        ("last_heartbeat", "INTEGER NOT NULL DEFAULT 0"),
        ("google_country", "TEXT"),
        ("warp_alive", "INTEGER NOT NULL DEFAULT 0"),
        ("last_restart", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for name, typ in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {typ}")
            log("INFO", f"migration: added nodes.{name}")


def db_init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = db_connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                name           TEXT PRIMARY KEY,
                token          TEXT NOT NULL,
                registered_at  INTEGER NOT NULL,
                last_heartbeat INTEGER NOT NULL DEFAULT 0,
                google_country TEXT,
                warp_alive     INTEGER NOT NULL DEFAULT 0,
                last_restart   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS commands (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                node         TEXT NOT NULL,
                command      TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                delivered_at INTEGER,
                attempts     INTEGER NOT NULL DEFAULT 0,
                ok           INTEGER,
                output       TEXT,
                result_at    INTEGER,
                not_before   INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_commands_pending
                ON commands(node, result_at, delivered_at);
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        _migrate_nodes(conn)
        _migrate_commands(conn)
        conn.execute("UPDATE commands SET attempts = 0 WHERE attempts IS NULL")
    finally:
        conn.close()


def meta_get(key, default=""):
    conn = db_connect()
    try:
        r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        conn.close()


def meta_set(key, value):
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
    finally:
        conn.close()


# ===========================================================================
# Telegram transport
# ===========================================================================


class TelegramAuthError(Exception):
    pass


class TelegramConflictError(Exception):
    pass


class TelegramTransportError(Exception):
    pass


def tg_call(method, http_timeout=40, **params):
    """Call the Telegram Bot API.

    http_timeout is the urllib socket timeout. Telegram's own long-poll
    timeout must be passed explicitly as `timeout=` and lands in **params.
    """
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise TelegramAuthError(f"{method}: 401 unauthorized") from e
        if e.code == 409:
            raise TelegramConflictError(f"{method}: 409 conflict") from e
        raise TelegramTransportError(f"{method}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise TelegramTransportError(f"{method}: {e.reason}") from e
    except Exception as e:
        raise TelegramTransportError(f"{method}: {type(e).__name__}: {e}") from e


def tg_send(text):
    """Send an HTML message to the operator. Returns True once Telegram
    accepted it; callers that must not lose a message check the result."""
    try:
        resp = tg_call(
            "sendMessage",
            http_timeout=20,
            chat_id=TG_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
        )
        return bool(resp and resp.get("ok"))
    except TelegramAuthError as e:
        log("ERROR", f"sendMessage: {e}")
    except TelegramTransportError as e:
        log("WARN", f"sendMessage: {e}")
    except TelegramConflictError:
        log("WARN", "sendMessage: 409 conflict")
    return False


# ===========================================================================
# Formatting
# ===========================================================================


def fmt_dur(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def node_state(row, t):
    """Judge a node from its last heartbeat: 'never', 'stale', 'warp_down', 'ok'.

    The stored warp_alive / google_country are only the node's *last report*.
    Once heartbeats stop they say nothing about the present, so every view of
    a node (Telegram, CLI, alerts) must go through this check first.
    """
    if not row["last_heartbeat"]:
        return "never"
    if t - row["last_heartbeat"] > STALE_AFTER:
        return "stale"
    if not row["warp_alive"]:
        return "warp_down"
    return "ok"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Validation of data reported by nodes (untrusted: any holder of a node token)
# ---------------------------------------------------------------------------

# Same alphabet as NODE_NAME in single/bin/warp-bot.sh.
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_IP_RE = re.compile(r"^[0-9A-Fa-f:.]{1,45}$")


def clean_country(value):
    """Upper-case two-letter country code, or '' for anything else."""
    if not isinstance(value, str):
        return ""
    value = value.strip().upper()
    return value if _COUNTRY_RE.match(value) else ""


def clean_ts(value):
    """Unix timestamp between 0 and now; anything else becomes 0."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return 0
    return ts if 0 <= ts <= now() else 0


def clean_meta(raw):
    """Whitelist the restart report a node sends.

    Only the keys the agent produces survive, each with a validated type, so
    a node cannot store arbitrary text or markup that is later rendered into
    the operator's Telegram chat.
    """
    if not isinstance(raw, dict):
        return {}
    ip = raw.get("exit_ip")
    return {
        "warp_up": raw.get("warp_up") is True,
        "still_ru": raw.get("still_ru") is True,
        "country": clean_country(raw.get("country")),
        "cloudflare_loc": clean_country(raw.get("cloudflare_loc")),
        "exit_ip": ip if isinstance(ip, str) and _IP_RE.match(ip) else "",
    }


def _format_restart_message(name, meta, kind):
    """Build the Telegram card for a restart event.

    kind: 'cmd#N restart' (manual) or 'auto-restart' (triggered by RU).
    """
    country = (meta.get("country") or "").strip()
    still_ru = bool(meta.get("still_ru"))
    warp_up = bool(meta.get("warp_up"))

    if not warp_up:
        icon = "🔴"
    elif still_ru:
        icon = "🟡"
    elif country and country != "?":
        icon = "🟢"
    else:
        icon = "🔴"

    lines = [f"{icon} <code>{esc(name)}</code> · {esc(kind)}"]
    if warp_up:
        lines.append(f"Google now: <b>{esc(country or '?')}</b>")
        cc = meta.get("cloudflare_loc")
        if cc:
            lines.append(f"Cloudflare loc: <code>{esc(cc)}</code>")
        ip = meta.get("exit_ip")
        if ip:
            lines.append(f"Exit IP: <code>{esc(ip)}</code>")
        if still_ru:
            lines.append("🔴 still RU")
    else:
        lines.append("⚠️ WARP did not come up")
    return "\n".join(lines)


# ===========================================================================
# Node state helpers
# ===========================================================================


def _apply_meta_to_node(name, meta):
    """Update the node's stored country and last_restart from a report."""
    country = (meta.get("country") or "").strip()
    if not country or country == "?":
        return
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE nodes SET google_country = ?, last_restart = ? WHERE name = ?",
            (country, now(), name),
        )
        conn.execute("DELETE FROM meta WHERE key = ?", (f"retry_unknown:{name}",))
    finally:
        conn.close()


def _schedule_unknown_retry(name):
    """Queue a delayed restart if the last one ended with country == '?'."""
    if RETRY_UNKNOWN_LIMIT <= 0:
        return
    key = f"retry_unknown:{name}"
    conn = db_connect()
    try:
        pending = conn.execute(
            "SELECT id FROM commands WHERE node = ? AND command = 'restart' "
            "AND result_at IS NULL LIMIT 1",
            (name,),
        ).fetchone()
        if pending:
            return
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        count = int(row["value"]) if row else 0

        if count >= RETRY_UNKNOWN_LIMIT:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            log("WARN", f"{name}: gave up after {RETRY_UNKNOWN_LIMIT} retries")
            tg_send(
                f"⚠️ <code>{esc(name)}</code>: Google country still unknown "
                f"after {RETRY_UNKNOWN_LIMIT} restarts, giving up"
            )
            return

        count += 1
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(count)),
        )
        t = now()
        conn.execute(
            "INSERT INTO commands (node, command, created_at, not_before) "
            "VALUES (?, 'restart', ?, ?)",
            (name, t, t + RETRY_UNKNOWN_DELAY),
        )
    finally:
        conn.close()
    log("INFO", f"{name}: queued retry #{count} in {RETRY_UNKNOWN_DELAY}s")
    tg_send(
        f"🔁 <code>{esc(name)}</code>: country still unknown, "
        f"retry #{count}/{RETRY_UNKNOWN_LIMIT} in {fmt_dur(RETRY_UNKNOWN_DELAY)}"
    )


# ===========================================================================
# Command handlers
# ===========================================================================

HELP_TEXT = (
    "<b>warp-coordinator</b>\n\n"
    "/nodes              — list nodes\n"
    "/status &lt;node&gt;     — details for one node\n"
    "/restart &lt;node&gt;    — queue restart\n"
    "/restart all        — queue restart for every node\n"
    "/logs &lt;node&gt;       — last command output\n"
    "/health             — coordinator health\n"
    "/help               — this message"
)


def cmd_nodes():
    conn = db_connect()
    try:
        rows = conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()
    finally:
        conn.close()
    if not rows:
        tg_send("No nodes registered.")
        return
    t = now()
    lines = []
    for r in rows:
        state = node_state(r, t)
        name = r["name"]
        if state == "never":
            icon, detail = "⚫", "never connected"
        elif state == "stale":
            icon, detail = "⚫", f"stale {fmt_dur(t - r['last_heartbeat'])}"
        elif state == "warp_down":
            icon, detail = "🔴", "WARP down"
        elif r["google_country"] == "RU":
            rest = t - r["last_restart"] if r["last_restart"] else 0
            icon = "🟡"
            detail = f"RU · restart {fmt_dur(rest)} ago" if rest else "RU"
        else:
            icon, detail = "🟢", (r["google_country"] or "?")
        lines.append(f"{icon} <code>{esc(name)}</code>  {esc(detail)}")
    tg_send("\n".join(lines))


def cmd_status(name):
    conn = db_connect()
    try:
        r = conn.execute("SELECT * FROM nodes WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    if not r:
        tg_send(f"Unknown node: <code>{esc(name)}</code>")
        return
    t = now()
    state = node_state(r, t)
    rest = t - r["last_restart"] if r["last_restart"] else 0
    last_warp = "yes" if r["warp_alive"] else "no"
    last_country = esc(r["google_country"] or "?")
    if state == "never":
        heartbeat = "never connected"
    else:
        heartbeat = f"{fmt_dur(t - r['last_heartbeat'])} ago"
    if state in ("never", "stale"):
        # No fresh report: do not present the last one as the current state.
        heartbeat += " ⚫ STALE" if state == "stale" else ""
        warp_line = f"warp_alive: unknown (last report: {last_warp})"
        google_line = f"google: unknown (last report: {last_country})"
    else:
        warp_line = f"warp_alive: {last_warp}"
        google_line = f"google: {last_country}"
    lines = [
        f"<b>{esc(name)}</b>",
        f"heartbeat: {heartbeat}",
        warp_line,
        google_line,
        f"last restart: {fmt_dur(rest)} ago" if rest else "last restart: never",
    ]
    tg_send("\n".join(lines))


def cmd_restart(name):
    expire_commands()  # an old command must not block a new one
    conn = db_connect()
    try:
        r = conn.execute("SELECT name FROM nodes WHERE name = ?", (name,)).fetchone()
        if not r:
            tg_send(f"Unknown node: <code>{esc(name)}</code>")
            return
        pending = conn.execute(
            "SELECT id FROM commands WHERE node = ? AND command = 'restart' "
            "AND result_at IS NULL LIMIT 1",
            (name,),
        ).fetchone()
        if pending:
            tg_send(f"Restart already pending for <code>{esc(name)}</code>")
            return
        cur = conn.execute(
            "INSERT INTO commands (node, command, created_at) VALUES (?, 'restart', ?)",
            (name, now()),
        )
        cid = cur.lastrowid
        node = conn.execute("SELECT * FROM nodes WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    log("INFO", f"queued restart #{cid} for {name}")
    text = f"Queued restart for <code>{esc(name)}</code> (#{cid})"
    state = node_state(node, now())
    if state in ("never", "stale"):
        text += (
            f"\n⚠️ The node is not reporting; the command expires in "
            f"{fmt_dur(COMMAND_TTL)} if it is not picked up."
        )
    tg_send(text)


def cmd_restart_all():
    expire_commands()
    conn = db_connect()
    try:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
        if not rows:
            tg_send("No nodes registered.")
            return
        t = now()
        queued, skipped = 0, []
        for r in rows:
            state = node_state(r, t)
            if state in ("never", "stale"):
                skipped.append(f"{r['name']} (not reporting)")
                continue
            pending = conn.execute(
                "SELECT id FROM commands WHERE node = ? AND command = 'restart' "
                "AND result_at IS NULL LIMIT 1",
                (r["name"],),
            ).fetchone()
            if pending:
                skipped.append(f"{r['name']} (already pending)")
                continue
            conn.execute(
                "INSERT INTO commands (node, command, created_at) VALUES (?, 'restart', ?)",
                (r["name"], t),
            )
            queued += 1
    finally:
        conn.close()
    log("INFO", f"queued restart for {queued} nodes, skipped {len(skipped)}")
    text = f"Queued restart for <b>{queued}</b> nodes"
    if skipped:
        text += "\nSkipped: " + ", ".join(f"<code>{esc(x)}</code>" for x in skipped)
    tg_send(text)


def cmd_logs(name):
    conn = db_connect()
    try:
        node = conn.execute("SELECT name FROM nodes WHERE name = ?", (name,)).fetchone()
        if not node:
            tg_send(f"Unknown node: <code>{esc(name)}</code>")
            return
        r = conn.execute(
            "SELECT id, command, ok, output FROM commands "
            "WHERE node = ? AND result_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    if not r:
        tg_send(f"Node <code>{esc(name)}</code> has no command history yet.")
        return
    icon = "✅" if r["ok"] else "❌"
    body = esc(r["output"] or "")[:3000]
    tg_send(
        f"{icon} <b>{esc(name)}</b> cmd#{r['id']} ({r['command']})\n<pre>{body}</pre>"
    )


def cmd_health():
    with _state_lock:
        s = dict(_state)
    ok = s.get("bot_alive", False)
    icon = "🟢" if ok else "🔴"
    age = now() - s.get("last_poll_ts", 0)
    lines = [
        f"{icon} bot_loop: {'alive' if ok else 'stalled'}",
        f"last_poll: {fmt_dur(age)} ago",
        f"offset: {s.get('offset', 0)}",
    ]
    tg_send("\n".join(lines))


def handle_command(text):
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@", 1)[0]
    args = parts[1:]

    log("INFO", f"tg command: {cmd} {args}")

    if cmd in ("/start", "/help"):
        tg_send(HELP_TEXT)
    elif cmd == "/nodes":
        cmd_nodes()
    elif cmd == "/status":
        cmd_status(args[0]) if args else cmd_nodes()
    elif cmd == "/restart":
        if not args:
            tg_send("Usage: /restart &lt;node&gt; | /restart all")
        elif args[0] == "all":
            cmd_restart_all()
        else:
            cmd_restart(args[0])
    elif cmd == "/logs":
        if args:
            cmd_logs(args[0])
        else:
            tg_send("Usage: /logs &lt;node&gt;")
    elif cmd == "/health":
        cmd_health()
    else:
        tg_send(f"Unknown command: <code>{esc(cmd)}</code>")


# ===========================================================================
# Bot loop
# ===========================================================================

_state = {
    "bot_alive": False,
    "last_poll_ts": 0,
    "offset": 0,
    "started_at": 0,
}
_state_lock = threading.Lock()


def _update_state(**kw):
    with _state_lock:
        _state.update(kw)


def bot_loop():
    offset = int(meta_get("tg_offset", "0") or "0")
    _update_state(bot_alive=True, offset=offset, started_at=now())
    log("INFO", f"bot_loop started, offset={offset}")

    backoff = 5
    backoff_max = 300
    stats_last = now()

    while True:
        try:
            data = tg_call(
                "getUpdates",
                http_timeout=POLL_TIMEOUT + 10,
                offset=offset,
                timeout=POLL_TIMEOUT,
                allowed_updates='["message"]',
            )
            backoff = 5
        except TelegramAuthError as e:
            log("ERROR", f"bot_loop: {e}; sleeping 3600s")
            _update_state(bot_alive=False)
            time.sleep(3600)
            continue
        except TelegramConflictError:
            log("WARN", f"bot_loop: 409 conflict; backoff {backoff}s")
            _update_state(bot_alive=False)
            time.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
            continue
        except TelegramTransportError as e:
            log("WARN", f"bot_loop: {e}; backoff {backoff}s")
            _update_state(bot_alive=False)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except Exception as e:
            log("ERROR", f"bot_loop unexpected: {type(e).__name__}: {e}")
            time.sleep(5)
            continue

        _update_state(bot_alive=True, last_poll_ts=now())

        if not data or not data.get("ok"):
            log("WARN", f"bot_loop: bad response: {data!r}")
            time.sleep(5)
            continue

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            try:
                meta_set("tg_offset", offset)
            except Exception as e:
                log("ERROR", f"meta_set offset failed: {e}")
            _update_state(offset=offset)

            msg = upd.get("message") or {}
            sender = (msg.get("from") or {}).get("id")
            text = msg.get("text", "")
            if sender != TG_CHAT_ID:
                log("DEBUG", f"ignoring message from {sender}")
                continue
            if not text:
                continue
            try:
                handle_command(text)
            except Exception as e:
                log("ERROR", f"handle_command error: {type(e).__name__}: {e}")
                with contextlib.suppress(Exception):
                    tg_send(f"⚠️ command failed: {esc(type(e).__name__)}")

        if now() - stats_last >= 3600:
            log("INFO", f"stats: offset={offset} healthy")
            stats_last = now()


# ===========================================================================
# Node monitor: tell the operator when a node goes silent or loses WARP
# ===========================================================================

# In-memory debounce: name -> (state, first seen in this state).
_node_seen = {}
_monitor_started = None


def _node_alert_text(name, row, state, told, t):
    n = f"<code>{esc(name)}</code>"
    if state == "stale":
        age = fmt_dur(t - row["last_heartbeat"])
        return (
            f"⚫ {n} stopped reporting (last heartbeat {age} ago). "
            "The node, its agent or its network is down."
        )
    if state == "warp_down":
        extra = " The node is reporting again." if told == "stale" else ""
        return f"🔴 {n}: the WARP proxy is down on the node.{extra}"
    if told == "stale":
        return f"🟢 {n} is reporting again."
    return f"🟢 {n}: the WARP proxy is back."


def node_monitor_tick(t=None):
    """One pass over all nodes. Sends one message per state change the
    operator has not been told about yet and returns how many were sent.

    * The state the operator was last told about is persisted (meta
      node_alert:<name>), so a repeat is never sent, including after a
      coordinator restart.
    * It is only updated after Telegram accepted the message, so an alert
      that could not be delivered is retried on the next pass.
    * Silence is not judged until STALE_AFTER seconds after start: nodes need
      that long to heartbeat again after a coordinator restart.
    * WARP-down must persist for WARP_DOWN_ALERT_AFTER seconds, so the short
      outage of an ordinary restart does not page anyone.
    """
    global _monitor_started
    t = now() if t is None else t
    if _monitor_started is None:
        _monitor_started = t

    conn = db_connect()
    try:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
    finally:
        conn.close()

    sent = 0
    for r in rows:
        name = r["name"]
        state = node_state(r, t)
        if state == "never":
            continue
        if state == "stale" and t - _monitor_started < STALE_AFTER:
            continue
        seen = _node_seen.get(name)
        if seen is None or seen[0] != state:
            seen = _node_seen[name] = (state, t)
        delay = WARP_DOWN_ALERT_AFTER if state == "warp_down" else 0
        if t - seen[1] < delay:
            continue
        key = f"node_alert:{name}"
        told = meta_get(key, "ok")
        if told == state:
            continue
        if tg_send(_node_alert_text(name, r, state, told, t)):
            meta_set(key, state)
            sent += 1
            log("WARN", f"alert sent: {name} {told} -> {state}")
        else:
            log("WARN", f"alert for {name} ({state}) not delivered; will retry")

    known = {r["name"] for r in rows}
    for name in [n for n in _node_seen if n not in known]:
        del _node_seen[name]
    return sent


def node_monitor():
    log("INFO", "node_monitor started")
    _update_state(monitor_ts=now())
    while True:
        time.sleep(MONITOR_INTERVAL)
        try:
            node_monitor_tick()
            _update_state(monitor_ts=now())
        except Exception as e:
            log("ERROR", f"node_monitor: {type(e).__name__}: {e}")


def deadman_ping():
    """Ping DEADMAN_URL if, and only if, the coordinator can still do its job.

    An external service (healthchecks.io, Uptime Kuma push monitor, ...)
    raises the alarm when the pings stop. So the ping is withheld while the
    Telegram path is broken (alerts could not reach you) and while the node
    monitor thread is stuck or dead, not just when the process is gone.
    Returns True if a ping was delivered.
    """
    if not DEADMAN_URL.startswith(("http://", "https://")):
        log("ERROR", "DEADMAN_URL must start with http:// or https://")
        return False
    with _state_lock:
        st = dict(_state)
    if not st.get("bot_alive", False):
        log("WARN", "deadman: not pinging, Telegram is unreachable")
        return False
    if NODE_ALERTS and now() - st.get("monitor_ts", 0) > 3 * MONITOR_INTERVAL + 30:
        log("WARN", "deadman: not pinging, node monitor is not running")
        return False
    try:
        with urllib.request.urlopen(DEADMAN_URL, timeout=10) as resp:
            resp.read(1)
        return True
    except Exception as e:
        log("WARN", f"deadman ping failed: {type(e).__name__}: {e}")
        return False


def deadman_loop():
    log("INFO", f"deadman ping every {DEADMAN_INTERVAL}s")
    while True:
        deadman_ping()
        time.sleep(DEADMAN_INTERVAL)


# ===========================================================================
# Command requeue watchdog
# ===========================================================================


def expire_commands(t=None):
    """Close commands that no node fetched within COMMAND_TTL.

    Without this a restart queued for a dead node stays pending forever:
    /restart answers "already pending", and the node runs the old command
    whenever it next shows up. Returns [(id, node), ...].
    """
    t = now() if t is None else t
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT id, node FROM commands "
            "WHERE result_at IS NULL AND delivered_at IS NULL "
            "AND MAX(created_at, not_before) < ?",
            (t - COMMAND_TTL,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE commands SET ok = 0, output = ?, result_at = ? WHERE id = ?",
                (f"expired: not fetched by the node within {fmt_dur(COMMAND_TTL)}", t, r["id"]),
            )
    finally:
        conn.close()
    return [(r["id"], r["node"]) for r in rows]


def prune_commands(t=None):
    """Delete finished commands older than COMMAND_RETENTION_DAYS."""
    t = now() if t is None else t
    conn = db_connect()
    try:
        cur = conn.execute(
            "DELETE FROM commands WHERE result_at IS NOT NULL AND result_at < ?",
            (t - COMMAND_RETENTION_DAYS * 86400,),
        )
        return cur.rowcount
    finally:
        conn.close()


_last_prune = 0


def requeue_watchdog():
    """Return commands whose lease expired to the pending queue, and
    permanently fail commands that exceeded the attempt limit."""
    log("INFO", "requeue_watchdog started")
    while True:
        time.sleep(30)
        try:
            t = now()
            cutoff = t - COMMAND_LEASE
            conn = db_connect()
            try:
                cur = conn.execute(
                    "UPDATE commands SET delivered_at = NULL "
                    "WHERE result_at IS NULL AND delivered_at IS NOT NULL "
                    "AND delivered_at < ? AND attempts < ?",
                    (cutoff, COMMAND_MAX_ATTEMPTS),
                )
                requeued = cur.rowcount

                cur = conn.execute(
                    "UPDATE commands SET ok = 0, output = ?, result_at = ? "
                    "WHERE result_at IS NULL AND attempts >= ?",
                    (LEASE_EXPIRED_OUTPUT, t, COMMAND_MAX_ATTEMPTS),
                )
                failed = cur.rowcount
            finally:
                conn.close()

            global _last_prune
            expired = expire_commands(t)
            if expired:
                log("WARN", f"expired {len(expired)} undelivered command(s)")
                tg_send(
                    "⌛ Command(s) expired undelivered: "
                    + ", ".join(f"<code>{esc(n)}</code> #{i}" for i, n in expired)
                )
            if t - _last_prune >= 3600:
                _last_prune = t
                pruned = prune_commands(t)
                if pruned:
                    log("INFO", f"pruned {pruned} old command(s)")

            if requeued:
                log("WARN", f"requeued {requeued} stale command(s)")
            if failed:
                log("ERROR", f"failed {failed} command(s): lease expired")
                tg_send(f"❌ {failed} command(s) failed: lease expired")
        except Exception as e:
            log("ERROR", f"requeue_watchdog: {type(e).__name__}: {e}")


# ===========================================================================
# HTTP API
# ===========================================================================


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "warp-coordinator/2.0"
    # Applied to the connection socket: without it a client that connects and
    # sends nothing (or a partial request) occupies a thread indefinitely.
    timeout = HTTP_SOCKET_TIMEOUT

    def log_message(self, *a):
        pass

    def fail(self, code, message):
        # The body of a rejected request was not consumed; drop the connection
        # instead of parsing leftover bytes as the next request.
        self.close_connection = True
        self.reply(code, {"error": message})

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise HttpError(400, "bad Content-Length")
        if n < 0:
            raise HttpError(400, "bad Content-Length")
        if n > MAX_BODY_BYTES:
            raise HttpError(413, "request body too large")
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def auth(self):
        name = self.headers.get("X-Node")
        token = self.headers.get("X-Token")
        if not name or not token:
            return None
        conn = db_connect()
        try:
            r = conn.execute(
                "SELECT token FROM nodes WHERE name = ?", (name,)
            ).fetchone()
        finally:
            conn.close()
        # compare_digest raises TypeError on non-ASCII str; compare bytes.
        if not r or not secrets.compare_digest(
            r["token"].encode("utf-8"), token.encode("utf-8")
        ):
            return None
        return name

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/health":
                self.r_health()
            elif path == "/commands":
                self.r_commands()
            else:
                self.reply(404, {"error": "not found"})
        except HttpError as e:
            self.fail(e.code, e.message)
        except Exception as e:
            log("ERROR", f"GET {path}: {type(e).__name__}: {e}")
            self.reply(500, {"error": "internal error"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/heartbeat":
                self.r_heartbeat()
            elif path == "/result":
                self.r_result()
            elif path == "/notify":
                self.r_notify()
            else:
                self.reply(404, {"error": "not found"})
        except HttpError as e:
            self.fail(e.code, e.message)
        except Exception as e:
            log("ERROR", f"POST {path}: {type(e).__name__}: {e}")
            self.reply(500, {"error": "internal error"})

    def r_health(self):
        with _state_lock:
            s = dict(_state)
        conn = db_connect()
        try:
            node_rows = conn.execute("SELECT * FROM nodes").fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM commands WHERE result_at IS NULL"
            ).fetchone()["c"]
        finally:
            conn.close()
        t = now()
        states = [node_state(r, t) for r in node_rows]
        nodes = len(node_rows)
        self.reply(
            200,
            {
                "ok": True,
                "bot_alive": s.get("bot_alive", False),
                "last_poll_age": now() - s.get("last_poll_ts", 0),
                "offset": s.get("offset", 0),
                "nodes": nodes,
                "nodes_stale": states.count("stale"),
                "nodes_warp_down": states.count("warp_down"),
                "commands_pending": pending,
                "retry_unknown_limit": RETRY_UNKNOWN_LIMIT,
            },
        )

    def r_heartbeat(self):
        name = self.auth()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return
        b = self.read_json()
        conn = db_connect()
        try:
            conn.execute(
                "UPDATE nodes SET last_heartbeat = ?, google_country = ?, "
                "warp_alive = ?, last_restart = ? WHERE name = ?",
                (
                    now(),
                    clean_country(b.get("google_country")) or None,
                    1 if b.get("warp_alive") is True else 0,
                    clean_ts(b.get("last_restart")),
                    name,
                ),
            )
        finally:
            conn.close()
        self.reply(200, {"ok": True})

    def r_commands(self):
        name = self.auth()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return
        t = now()
        cutoff = t - COMMAND_LEASE
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT id, command FROM commands "
                "WHERE node = ? AND result_at IS NULL "
                "  AND not_before <= ? "
                "  AND MAX(created_at, not_before) >= ? "
                "  AND (delivered_at IS NULL OR delivered_at < ?) "
                "  AND attempts < ? "
                "ORDER BY id LIMIT 5",
                (name, t, t - COMMAND_TTL, cutoff, COMMAND_MAX_ATTEMPTS),
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                ph = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE commands SET delivered_at = ?, attempts = attempts + 1 "
                    f"WHERE id IN ({ph})",
                    [t, *ids],
                )
                log("INFO", f"delivered {len(rows)} command(s) to {name}")
        finally:
            conn.close()
        self.reply(
            200, {"commands": [{"id": r["id"], "cmd": r["command"]} for r in rows]}
        )

    def r_result(self):
        name = self.auth()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return

        b = self.read_json()
        cid = b.get("id")
        ok = b.get("ok") is True
        out = str(b.get("output", ""))[:4000]
        meta = clean_meta(b.get("meta"))
        if type(cid) is not int:
            self.reply(200, {"ok": True, "ignored": True})
            return

        # Record the result exactly once. The agent re-sends a result when the
        # response was lost, so a repeat must not notify, update the node or
        # schedule another retry. The only closed command that may be
        # overwritten is one the watchdog failed by timeout: the real outcome
        # is more useful than the synthetic failure.
        conn = db_connect()
        try:
            cur = conn.execute(
                "UPDATE commands SET ok = ?, output = ?, result_at = ? "
                "WHERE id = ? AND node = ? "
                "AND (result_at IS NULL OR output = ?)",
                (1 if ok else 0, out, now(), cid, name, LEASE_EXPIRED_OUTPUT),
            )
            recorded = cur.rowcount == 1
        finally:
            conn.close()

        if not recorded:
            log("INFO", f"ignored result for cmd#{cid} from {name}: not open")
            # 200 so the agent drops it from its pending queue instead of
            # resending the same payload forever.
            self.reply(200, {"ok": True, "ignored": True})
            return

        _apply_meta_to_node(name, meta)

        country = (meta.get("country") or "").strip()
        log(
            "INFO",
            f"result cmd#{cid} from {name}: ok={ok} country={country or '?'}",
        )

        tg_send(_format_restart_message(name, meta, f"cmd#{cid} restart"))

        if ok and meta.get("warp_up") and (not country or country == "?"):
            _schedule_unknown_retry(name)

        self.reply(200, {"ok": True})

    def r_notify(self):
        name = self.auth()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return

        b = self.read_json()
        text = str(b.get("text", ""))
        raw_meta = b.get("meta")
        meta = clean_meta(raw_meta)
        reason = str(b.get("reason", ""))

        _apply_meta_to_node(name, meta)

        if reason == "auto" and isinstance(raw_meta, dict) and raw_meta:
            tg_send(_format_restart_message(name, meta, "auto-restart"))
        elif text:
            tg_send(f"[<code>{esc(name)}</code>] {esc(text)}")

        self.reply(200, {"ok": True})


# ===========================================================================
# CLI
# ===========================================================================


def cli_add_node(name):
    if not NODE_NAME_RE.match(name):
        raise ValueError("node name must match [A-Za-z0-9._-]{1,64}")
    token = secrets.token_urlsafe(32)
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO nodes (name, token, registered_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET token = excluded.token",
            (name, token, now()),
        )
    finally:
        conn.close()
    print(f"NODE_NAME={name}")
    print(f"NODE_TOKEN={token}")


def cli_remove_node(name):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM nodes WHERE name = ?", (name,))
        conn.execute("DELETE FROM commands WHERE node = ?", (name,))
        for prefix in ("retry_unknown", "node_alert"):
            conn.execute("DELETE FROM meta WHERE key = ?", (f"{prefix}:{name}",))
    finally:
        conn.close()
    print(f"Removed {name}")


def cli_list_nodes():
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT name, last_heartbeat, google_country, warp_alive "
            "FROM nodes ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("(no nodes)")
        return
    t = now()
    for r in rows:
        state = node_state(r, t)
        country = r["google_country"] or "?"
        alive = "yes" if r["warp_alive"] else "no"
        if state == "never":
            detail = "hb=never"
        elif state == "stale":
            detail = (
                f"hb={fmt_dur(t - r['last_heartbeat'])} ago "
                f"(last report: country={country} alive={alive})"
            )
        else:
            detail = (
                f"hb={fmt_dur(t - r['last_heartbeat'])} "
                f"country={country} alive={alive}"
            )
        label = {"never": "NEVER", "stale": "STALE", "warp_down": "WARP-DOWN"}.get(
            state, "OK"
        )
        print(f"{r['name']:30s} {label:9s} {detail}")


# ===========================================================================
# Server / main
# ===========================================================================


def sanity_check():
    try:
        me = tg_call("getMe", http_timeout=10)
        if me and me.get("ok"):
            log(
                "INFO", f"telegram bot @{(me.get('result') or {}).get('username', '?')}"
            )
    except TelegramAuthError:
        print("FATAL: TG_TOKEN is invalid", file=sys.stderr)
        sys.exit(1)
    except TelegramTransportError as e:
        log("WARN", f"cannot reach Telegram at startup: {e}")

    try:
        info = tg_call("getWebhookInfo", http_timeout=10)
        if info and info.get("ok"):
            url = (info.get("result") or {}).get("url") or ""
            if url:
                log("ERROR", f"webhook is set to {url!r}; getUpdates will 409")
                log("ERROR", "run: deleteWebhook?drop_pending_updates=true")
    except Exception:
        pass


def run_server():
    setup_logging(LOG_LEVEL)
    load_tg_config()
    db_init()
    sanity_check()

    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=requeue_watchdog, daemon=True).start()
    if NODE_ALERTS:
        threading.Thread(target=node_monitor, daemon=True).start()
    if DEADMAN_URL:
        threading.Thread(target=deadman_loop, daemon=True).start()

    if HTTP_HOST in ("0.0.0.0", "::"):
        log(
            "WARN",
            "HTTP API listens on all interfaces over plain HTTP; node tokens "
            "travel in cleartext. Set HTTP_HOST to a private/WireGuard address "
            "or firewall the port (see docs/fleet.md).",
        )
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    server.daemon_threads = True
    log("INFO", f"listening on {HTTP_HOST}:{HTTP_PORT}")

    def shutdown(sig, frame):
        log("INFO", f"signal {sig}, shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()
    log("INFO", "stopped")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print(
            "Usage:\n  coordinator.py run\n  coordinator.py add-node <name>\n"
            "  coordinator.py remove-node <name>\n  coordinator.py list-nodes"
        )
        return 1
    cmd = sys.argv[1]
    if cmd == "run":
        run_server()
    elif cmd == "add-node" and len(sys.argv) == 3:
        db_init()
        try:
            cli_add_node(sys.argv[2])
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    elif cmd == "remove-node" and len(sys.argv) == 3:
        db_init()
        cli_remove_node(sys.argv[2])
    elif cmd == "list-nodes":
        db_init()
        cli_list_nodes()
    else:
        print(f"unknown command: {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
