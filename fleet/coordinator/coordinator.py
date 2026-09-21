#!/usr/bin/env python3
"""warp-coordinator — Telegram bot + HTTP API for warp-agents.

Design notes:
  * Bot long-polling runs in the main thread. If it dies, the process dies.
  * HTTP server runs in a daemon thread.
  * All external calls have explicit timeouts.
  * All state changes are logged.
  * /health endpoint exposes internal state for external monitoring.
  * Commands have a lease; undelivered results are re-queued after
    COMMAND_LEASE seconds, up to COMMAND_MAX_ATTEMPTS times.
"""

import contextlib
import json
import logging
import os
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
DB_PATH = env("DB_PATH", "/var/lib/warp-coordinator/coordinator.db")
STALE_AFTER = int(env("STALE_AFTER", "180"))
COMMAND_LEASE = int(env("COMMAND_LEASE", "120"))
COMMAND_MAX_ATTEMPTS = int(env("COMMAND_MAX_ATTEMPTS", "3"))
POLL_TIMEOUT = int(env("POLL_TIMEOUT", "25"))
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


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
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


def db_init():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
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
                result_at    INTEGER
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
        # Backfill: старые записи без attempts
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


def tg_call(method, timeout=40, **params):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    try:
        tg_call(
            "sendMessage",
            timeout=20,
            chat_id=TG_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview="true",
        )
    except TelegramAuthError as e:
        log("ERROR", f"sendMessage: {e}")
    except TelegramTransportError as e:
        log("WARN", f"sendMessage: {e}")
    except TelegramConflictError:
        # Should not happen for sendMessage, but catch anyway.
        log("WARN", "sendMessage: 409 conflict")


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


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        age = t - r["last_heartbeat"] if r["last_heartbeat"] else 999999
        name = r["name"]
        if age > STALE_AFTER:
            icon, detail = "⚫", f"stale {fmt_dur(age)}"
        elif not r["warp_alive"]:
            icon, detail = "🔴", "WARP down"
        elif r["google_country"] == "RU":
            rest = t - r["last_restart"] if r["last_restart"] else 0
            icon = "🟡"
            detail = f"RU · restart {fmt_dur(rest)} ago" if rest else "RU"
        else:
            icon, detail = "🟢", (r["google_country"] or "?")
        lines.append(f"{icon} <code>{name}</code>  {detail}")
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
    age = t - r["last_heartbeat"] if r["last_heartbeat"] else 0
    rest = t - r["last_restart"] if r["last_restart"] else 0
    lines = [
        f"<b>{esc(name)}</b>",
        f"heartbeat: {fmt_dur(age)} ago",
        f"warp_alive: {'yes' if r['warp_alive'] else 'no'}",
        f"google: {r['google_country'] or '?'}",
        f"last restart: {fmt_dur(rest)} ago" if rest else "last restart: never",
    ]
    tg_send("\n".join(lines))


def cmd_restart(name):
    conn = db_connect()
    try:
        r = conn.execute("SELECT name FROM nodes WHERE name = ?", (name,)).fetchone()
        if not r:
            tg_send(f"Unknown node: <code>{esc(name)}</code>")
            return
        # Deduplicate: skip if an identical command is already pending.
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
    finally:
        conn.close()
    log("INFO", f"queued restart #{cid} for {name}")
    tg_send(f"Queued restart for <code>{esc(name)}</code> (#{cid})")


def cmd_restart_all():
    conn = db_connect()
    try:
        rows = conn.execute("SELECT name FROM nodes").fetchall()
        if not rows:
            tg_send("No nodes registered.")
            return
        t = now()
        for r in rows:
            conn.execute(
                "INSERT INTO commands (node, command, created_at) VALUES (?, 'restart', ?)",
                (r["name"], t),
            )
        n = len(rows)
    finally:
        conn.close()
    log("INFO", f"queued restart for {n} nodes")
    tg_send(f"Queued restart for <b>{n}</b> nodes")


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
                timeout=POLL_TIMEOUT + 10,
                offset=offset,
                timeout_s=POLL_TIMEOUT,
                allowed_updates='["message"]',
            )
            backoff = 5
        except TelegramAuthError as e:
            log("ERROR", f"bot_loop: {e}; sleeping 3600s")
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
# Command requeue watchdog
# ===========================================================================


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
                # expired lease → clear delivered_at so agent can pick again
                cur = conn.execute(
                    "UPDATE commands SET delivered_at = NULL "
                    "WHERE result_at IS NULL AND delivered_at IS NOT NULL "
                    "AND delivered_at < ? AND attempts < ?",
                    (cutoff, COMMAND_MAX_ATTEMPTS),
                )
                requeued = cur.rowcount

                # exceeded attempts → mark failed
                cur = conn.execute(
                    "UPDATE commands SET ok = 0, output = ?, result_at = ? "
                    "WHERE result_at IS NULL AND attempts >= ?",
                    ("lease expired after max attempts", t, COMMAND_MAX_ATTEMPTS),
                )
                failed = cur.rowcount
            finally:
                conn.close()

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


class Handler(BaseHTTPRequestHandler):
    server_version = "warp-coordinator/2.0"

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length", "0"))
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
        if not r or not secrets.compare_digest(r["token"], token):
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
        except Exception as e:
            log("ERROR", f"GET {path}: {type(e).__name__}: {e}")
            self.reply(500, {"error": str(e)})

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
        except Exception as e:
            log("ERROR", f"POST {path}: {type(e).__name__}: {e}")
            self.reply(500, {"error": str(e)})

    def r_health(self):
        with _state_lock:
            s = dict(_state)
        conn = db_connect()
        try:
            nodes = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM commands WHERE result_at IS NULL"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.reply(
            200,
            {
                "ok": True,
                "bot_alive": s.get("bot_alive", False),
                "last_poll_age": now() - s.get("last_poll_ts", 0),
                "offset": s.get("offset", 0),
                "nodes": nodes,
                "commands_pending": pending,
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
                    b.get("google_country") or None,
                    1 if b.get("warp_alive") else 0,
                    int(b.get("last_restart") or 0),
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
                "  AND (delivered_at IS NULL OR delivered_at < ?) "
                "  AND attempts < ? "
                "ORDER BY id LIMIT 5",
                (name, cutoff, COMMAND_MAX_ATTEMPTS),
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
        ok = bool(b.get("ok"))
        out = str(b.get("output", ""))[:4000]
        conn = db_connect()
        try:
            conn.execute(
                "UPDATE commands SET ok = ?, output = ?, result_at = ? "
                "WHERE id = ? AND node = ?",
                (1 if ok else 0, out, now(), cid, name),
            )
        finally:
            conn.close()
        log("INFO", f"result cmd#{cid} from {name}: ok={ok}")
        icon = "✅" if ok else "❌"
        tg_send(
            f"{icon} <code>{esc(name)}</code> cmd#{cid} finished\n<pre>{esc(out)[:1500]}</pre>"
        )
        self.reply(200, {"ok": True})

    def r_notify(self):
        name = self.auth()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return
        b = self.read_json()
        text = str(b.get("text", ""))
        if text:
            tg_send(f"[<code>{esc(name)}</code>] {esc(text)}")
        self.reply(200, {"ok": True})


# ===========================================================================
# CLI
# ===========================================================================


def cli_add_node(name):
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
        age = t - r["last_heartbeat"] if r["last_heartbeat"] else -1
        age_s = f"{age}s" if age >= 0 else "never"
        print(
            f"{r['name']:30s} hb={age_s:>8s} "
            f"country={r['google_country'] or '?':>3s} "
            f"alive={'yes' if r['warp_alive'] else 'no'}"
        )


# ===========================================================================
# Server / main
# ===========================================================================


def sanity_check():
    try:
        me = tg_call("getMe", timeout=10)
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
        info = tg_call("getWebhookInfo", timeout=10)
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
        cli_add_node(sys.argv[2])
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
