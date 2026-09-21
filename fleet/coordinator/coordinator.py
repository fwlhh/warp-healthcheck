#!/usr/bin/env python3
"""warp-coordinator — central bot + HTTP API for warp-agents."""

import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        print(f"FATAL: {key} is not set", file=sys.stderr)
        sys.exit(1)
    return val or ""


# Non-secret settings, safe to read at import time.
HTTP_HOST     = env("HTTP_HOST", "0.0.0.0")
HTTP_PORT     = int(env("HTTP_PORT", "8080"))
DB_PATH       = env("DB_PATH", "/var/lib/warp-coordinator/coordinator.db")
STALE_AFTER   = int(env("STALE_AFTER", "180"))
TG_OFFSET_KEY = "tg_offset"

# Secrets, loaded lazily so CLI subcommands do not require them.
TG_TOKEN: str = ""
TG_CHAT_ID: int = 0


def load_telegram_config() -> None:
    global TG_TOKEN, TG_CHAT_ID
    TG_TOKEN = env("TG_TOKEN", required=True)
    raw = env("TG_CHAT_ID", required=True)
    try:
        TG_CHAT_ID = int(raw)
    except ValueError:
        print(f"FATAL: TG_CHAT_ID must be an integer, got {raw!r}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def now() -> int:
    return int(time.time())


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def db_init() -> None:
    dbdir = os.path.dirname(DB_PATH)
    if dbdir:
        os.makedirs(dbdir, exist_ok=True)
    conn = db_connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            name            TEXT PRIMARY KEY,
            token           TEXT NOT NULL,
            registered_at   INTEGER NOT NULL,
            last_heartbeat  INTEGER NOT NULL DEFAULT 0,
            google_country  TEXT,
            warp_alive      INTEGER NOT NULL DEFAULT 0,
            last_restart    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS commands (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            node         TEXT NOT NULL,
            command      TEXT NOT NULL,
            created_at   INTEGER NOT NULL,
            delivered_at INTEGER,
            result       TEXT,
            result_at    INTEGER
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.close()


def get_meta(key: str) -> str:
    conn = db_connect()
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return r["value"] if r else ""


def set_meta(key: str, value: str) -> None:
    conn = db_connect()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.close()


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def tg_api(method: str, **params):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # 409 on getUpdates is expected when another poller holds the token.
        # Do not spam the log; bot_loop applies backoff.
        if not (method == "getUpdates" and e.code == 409):
            print(f"tg_api {method} http {e.code}", flush=True)
        return None
    except Exception as e:
        print(f"tg_api {method} failed: {type(e).__name__}: {e}", flush=True)
        return None


def tg_send(text: str) -> None:
    tg_api(
        "sendMessage",
        chat_id=TG_CHAT_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview="true",
    )


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ---------------------------------------------------------------------------
# Telegram sanity check (startup only)
# ---------------------------------------------------------------------------

def sanity_check_telegram() -> None:
    """Warn on startup if the token is bad, a webhook is set, or the bot
    is unreachable."""
    me = tg_api("getMe")
    if me and me.get("ok"):
        username = (me.get("result") or {}).get("username", "?")
        print(f"telegram bot @{username}", flush=True)
    else:
        print(
            "WARNING: cannot reach Telegram — check TG_TOKEN and network",
            flush=True,
        )

    info = tg_api("getWebhookInfo")
    if info and info.get("ok"):
        url = (info.get("result") or {}).get("url") or ""
        if url:
            print(
                f"WARNING: webhook is set to {url!r}; getUpdates will fail with 409",
                flush=True,
            )
            print(
                "         run: "
                f"curl 'https://api.telegram.org/bot$TG_TOKEN/deleteWebhook?drop_pending_updates=true'",
                flush=True,
            )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "<b>warp-coordinator</b>\n\n"
    "/nodes                     — list all nodes\n"
    "/status &lt;node&gt;           — details for one node\n"
    "/restart &lt;node&gt;          — queue restart\n"
    "/restart all               — queue restart for every node\n"
    "/logs &lt;node&gt;             — last command result\n"
    "/help                      — this message"
)


def show_all_nodes() -> None:
    conn = db_connect()
    rows = conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()
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
            icon = "⚫"
            detail = f"stale {fmt_duration(age)}"
        elif not r["warp_alive"]:
            icon = "🔴"
            detail = "WARP down"
        elif r["google_country"] == "RU":
            rest = t - r["last_restart"] if r["last_restart"] else 0
            icon = "🟡"
            detail = f"RU · restart {fmt_duration(rest)} ago" if rest else "RU"
        else:
            icon = "🟢"
            detail = r["google_country"] or "?"
        lines.append(f"{icon} <code>{name}</code>  {detail}")
    tg_send("\n".join(lines))


def show_node_detail(name: str) -> None:
    conn = db_connect()
    r = conn.execute("SELECT * FROM nodes WHERE name = ?", (name,)).fetchone()
    conn.close()
    if not r:
        tg_send(f"Unknown node: <code>{name}</code>")
        return
    t = now()
    age = t - r["last_heartbeat"] if r["last_heartbeat"] else 0
    rest = t - r["last_restart"] if r["last_restart"] else 0
    body = (
        f"<b>{name}</b>\n"
        f"heartbeat: {fmt_duration(age)} ago\n"
        f"warp_alive: {'yes' if r['warp_alive'] else 'no'}\n"
        f"google: {r['google_country'] or '?'}\n"
    )
    body += f"last restart: {fmt_duration(rest)} ago" if rest else "last restart: never"
    tg_send(body)


def queue_restart(name: str) -> None:
    conn = db_connect()
    r = conn.execute("SELECT name FROM nodes WHERE name = ?", (name,)).fetchone()
    if not r:
        conn.close()
        tg_send(f"Unknown node: <code>{name}</code>")
        return
    conn.execute(
        "INSERT INTO commands (node, command, created_at) VALUES (?, ?, ?)",
        (name, "restart", now()),
    )
    conn.close()
    tg_send(f"Queued restart for <code>{name}</code>")


def queue_restart_all() -> None:
    conn = db_connect()
    rows = conn.execute("SELECT name FROM nodes").fetchall()
    if not rows:
        conn.close()
        tg_send("No nodes registered.")
        return
    t = now()
    for r in rows:
        conn.execute(
            "INSERT INTO commands (node, command, created_at) VALUES (?, ?, ?)",
            (r["name"], "restart", t),
        )
    conn.close()
    tg_send(f"Queued restart for <b>{len(rows)}</b> nodes")


def show_logs(name: str) -> None:
    conn = db_connect()

    node = conn.execute(
        "SELECT name FROM nodes WHERE name = ?", (name,)
    ).fetchone()
    if not node:
        conn.close()
        tg_send(f"Unknown node: <code>{name}</code>")
        return

    r = conn.execute(
        "SELECT id, command, result FROM commands "
        "WHERE node = ? AND result IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    conn.close()

    if not r:
        tg_send(
            f"Node <code>{name}</code> has no command history yet. "
            f"Try <code>/restart {name}</code>."
        )
        return

    try:
        payload = json.loads(r["result"])
        ok = payload.get("ok")
        out = payload.get("output", "")
    except Exception:
        ok, out = False, str(r["result"])

    icon = "✅" if ok else "❌"
    safe = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    tg_send(
        f"{icon} <b>{name}</b> cmd#{r['id']} ({r['command']})\n"
        f"<pre>{safe[:3000]}</pre>"
    )


def handle_command(text: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@", 1)[0]
    args = parts[1:]

    if cmd in ("/start", "/help"):
        tg_send(HELP_TEXT)
    elif cmd == "/nodes":
        show_all_nodes()
    elif cmd == "/status":
        if args:
            show_node_detail(args[0])
        else:
            show_all_nodes()
    elif cmd == "/restart":
        if not args:
            tg_send("Usage: /restart &lt;node&gt; | /restart all")
            return
        if args[0] == "all":
            queue_restart_all()
        else:
            queue_restart(args[0])
    elif cmd == "/logs":
        if not args:
            tg_send("Usage: /logs &lt;node&gt;")
            return
        show_logs(args[0])
    else:
        tg_send(f"Unknown command: <code>{cmd}</code>\n\n{HELP_TEXT}")


# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------

def bot_loop() -> None:
    offset = int(get_meta(TG_OFFSET_KEY) or "0")
    print(f"bot_loop started, offset={offset}", flush=True)

    backoff = 5
    backoff_max = 300

    while True:
        try:
            resp = tg_api(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates='["message"]',
            )
        except Exception as e:
            print(
                f"bot_loop getUpdates crashed: {type(e).__name__}: {e}",
                flush=True,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
            continue

        if not resp or not resp.get("ok"):
            print(f"bot_loop backoff {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
            continue

        backoff = 5

        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            try:
                set_meta(TG_OFFSET_KEY, str(offset))
                msg = upd.get("message") or {}
                sender = (msg.get("from") or {}).get("id")
                if sender != TG_CHAT_ID:
                    continue
                text = msg.get("text", "")
                if not text:
                    continue
                handle_command(text)
            except Exception as e:
                print(
                    f"bot_loop: failed to handle update "
                    f"{upd.get('update_id')}: {type(e).__name__}: {e}",
                    flush=True,
                )


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "warp-coordinator/1.0"

    def log_message(self, *args):
        pass

    def reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def authenticate(self):
        name = self.headers.get("X-Node")
        token = self.headers.get("X-Token")
        if not name or not token:
            return None
        conn = db_connect()
        r = conn.execute(
            "SELECT token FROM nodes WHERE name = ?", (name,)
        ).fetchone()
        conn.close()
        if not r or not secrets.compare_digest(r["token"], token):
            return None
        return name

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/commands":
                self.route_commands()
            else:
                self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/heartbeat":
                self.route_heartbeat()
            elif path == "/result":
                self.route_result()
            elif path == "/notify":
                self.route_notify()
            else:
                self.reply(404, {"error": "not found"})
        except Exception as e:
            self.reply(500, {"error": str(e)})

    def route_heartbeat(self) -> None:
        name = self.authenticate()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return
        body = self.read_json()
        conn = db_connect()
        conn.execute(
            """
            UPDATE nodes SET
              last_heartbeat = ?,
              google_country = ?,
              warp_alive     = ?,
              last_restart   = ?
            WHERE name = ?
            """,
            (
                now(),
                body.get("google_country") or None,
                1 if body.get("warp_alive") else 0,
                int(body.get("last_restart") or 0),
                name,
            ),
        )
        conn.close()
        self.reply(200, {"ok": True})

    def route_commands(self) -> None:
        name = self.authenticate()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return
        conn = db_connect()
        rows = conn.execute(
            "SELECT id, command FROM commands "
            "WHERE node = ? AND delivered_at IS NULL "
            "ORDER BY id",
            (name,),
        ).fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE commands SET delivered_at = ? WHERE id IN ({placeholders})",
                [now(), *ids],
            )
        conn.close()
        self.reply(
            200,
            {"commands": [{"id": r["id"], "cmd": r["command"]} for r in rows]},
        )

    def route_result(self) -> None:
        name = self.authenticate()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return
        body = self.read_json()
        cmd_id = body.get("id")
        ok = bool(body.get("ok"))
        output = str(body.get("output", ""))[:4000]
        conn = db_connect()
        conn.execute(
            "UPDATE commands SET result = ?, result_at = ? "
            "WHERE id = ? AND node = ?",
            (json.dumps({"ok": ok, "output": output}), now(), cmd_id, name),
        )
        conn.close()
        icon = "✅" if ok else "❌"
        safe = output.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        tg_send(
            f"{icon} <code>{name}</code> cmd#{cmd_id} finished\n"
            f"<pre>{safe[:1500]}</pre>"
        )
        self.reply(200, {"ok": True})

    def route_notify(self) -> None:
        name = self.authenticate()
        if not name:
            self.reply(401, {"error": "unauthorized"})
            return
        body = self.read_json()
        text = str(body.get("text", ""))
        if text:
            tg_send(f"[<code>{name}</code>] {text}")
        self.reply(200, {"ok": True})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_add_node(name: str) -> None:
    token = secrets.token_urlsafe(32)
    conn = db_connect()
    conn.execute(
        "INSERT INTO nodes (name, token, registered_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET token = excluded.token",
        (name, token, now()),
    )
    conn.close()
    print(f"NODE_NAME={name}")
    print(f"NODE_TOKEN={token}")


def cli_remove_node(name: str) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM nodes WHERE name = ?", (name,))
    conn.execute("DELETE FROM commands WHERE node = ?", (name,))
    conn.close()
    print(f"Removed {name}")


def cli_list_nodes() -> None:
    conn = db_connect()
    rows = conn.execute(
        "SELECT name, last_heartbeat, google_country, warp_alive "
        "FROM nodes ORDER BY name"
    ).fetchall()
    conn.close()
    if not rows:
        print("(no nodes)")
        return
    t = now()
    for r in rows:
        age = t - r["last_heartbeat"] if r["last_heartbeat"] else -1
        age_s = f"{age}s" if age >= 0 else "never"
        print(
            f"{r['name']:30s}  hb={age_s:>8s}  "
            f"country={r['google_country'] or '?':>3s}  "
            f"alive={'yes' if r['warp_alive'] else 'no'}"
        )


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def run_server() -> None:
    load_telegram_config()
    db_init()
    sanity_check_telegram()

    threading.Thread(target=bot_loop, daemon=True).start()

    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    server.daemon_threads = True
    print(f"listening on {HTTP_HOST}:{HTTP_PORT}", flush=True)
    server.serve_forever()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage:")
        print("  coordinator.py run")
        print("  coordinator.py add-node <name>")
        print("  coordinator.py remove-node <name>")
        print("  coordinator.py list-nodes")
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