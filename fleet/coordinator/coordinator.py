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
import hashlib
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
RETRY_UNKNOWN_DELAY = int(env("RETRY_UNKNOWN_DELAY", "90"))
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
# Countries where Google refuses the service; shown as a warning (🟡) in /nodes
# and /status. Use the same value as the agents' BAD_COUNTRIES.
BAD_COUNTRIES = frozenset(env("BAD_COUNTRIES", "RU").replace(",", " ").upper().split())
# The name a node's only WARP gets when its agent does not report a list.
DEFAULT_WARP = "warp"
MAX_WARPS_PER_NODE = 64

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
        # Which WARP of the node a command is for; '' = the whole node.
        ("target", "TEXT NOT NULL DEFAULT ''"),
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


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def token_digest(token: str) -> str:
    """What the database stores for a node token.

    Tokens are 256 bits of randomness, so a plain SHA-256 is sufficient (a
    slow password hash would only cost CPU on every request). A copy of the
    database therefore cannot be used to impersonate a node, and the digest
    itself is not accepted as a token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _secure_storage() -> None:
    """Keep the database private to its owner.

    The file is created with 0600 before SQLite touches it, so there is no
    window with umask-derived permissions, and an existing database is
    tightened. SQLite gives -wal/-shm the same mode as the main file. The
    parent directory is only restricted when we create it: an existing one
    belongs to the administrator.
    """
    parent = DB_PATH.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created:
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)
    os.close(os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600))
    with contextlib.suppress(OSError):
        os.chmod(DB_PATH, 0o600)


def _migrate_hash_tokens(conn) -> None:
    """One-way: replace plaintext node tokens by their digest.

    Older versions stored tokens verbatim and cannot read digests, so a copy
    of the database is made first (once) for a rollback.
    """
    rows = conn.execute("SELECT name, token FROM nodes").fetchall()
    plain = [r for r in rows if not _DIGEST_RE.match(r["token"])]
    if not plain:
        return
    backup = DB_PATH.with_name(DB_PATH.name + ".pre-token-hash")
    if not backup.exists():
        os.close(os.open(backup, os.O_CREAT | os.O_RDWR, 0o600))
        dst = sqlite3.connect(str(backup))
        try:
            conn.backup(dst)
        finally:
            dst.close()
        with contextlib.suppress(OSError):
            os.chmod(backup, 0o600)
    for r in plain:
        conn.execute(
            "UPDATE nodes SET token = ? WHERE name = ?",
            (token_digest(r["token"]), r["name"]),
        )
    log(
        "INFO",
        f"migration: stored {len(plain)} node token(s) hashed. A copy of the "
        f"previous database, with plaintext tokens, is at {backup}: restore it "
        "to roll back to an older version, and delete it once the fleet works.",
    )


def db_init() -> None:
    _secure_storage()
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
                not_before   INTEGER NOT NULL DEFAULT 0,
                target       TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS warps (
                node           TEXT NOT NULL,
                name           TEXT NOT NULL,
                google_country TEXT,
                warp_alive     INTEGER NOT NULL DEFAULT 0,
                last_restart   INTEGER NOT NULL DEFAULT 0,
                updated_at     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (node, name)
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
        _migrate_hash_tokens(conn)
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


def node_state(row, t, warps=None):
    """Judge a node from its last heartbeat: 'never', 'stale', 'warp_down', 'ok'.

    The stored warp_alive / google_country are only the node's *last report*.
    Once heartbeats stop they say nothing about the present, so every view of
    a node (Telegram, CLI, alerts) must go through this check first. With
    `warps` (see node_warps) a node is 'warp_down' when any of them is down.
    """
    if not row["last_heartbeat"]:
        return "never"
    if t - row["last_heartbeat"] > STALE_AFTER:
        return "stale"
    if warps is not None:
        down = any(not w["warp_alive"] for w in warps)
    else:
        down = not row["warp_alive"]
    return "warp_down" if down else "ok"


def node_warps(row, conn=None):
    """The WARPs a node reported, in the order its agent lists them.

    A node whose agent does not report a list (an older agent) has one
    implicit WARP, built from the node's own columns, so every view can treat
    all nodes alike.
    """
    own = conn is None
    conn = conn or db_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM warps WHERE node = ? ORDER BY rowid", (row["name"],)
        ).fetchall()
    finally:
        if own:
            conn.close()
    if rows:
        return [dict(r) for r in rows]
    return [
        {
            "node": row["name"],
            "name": DEFAULT_WARP,
            "google_country": row["google_country"],
            "warp_alive": row["warp_alive"],
            "last_restart": row["last_restart"],
            "updated_at": row["last_heartbeat"],
            "implicit": True,
        }
    ]


def warp_label(node, warp_name, multi):
    """'NE01 / warp2'. The WARP is left out when it is the node's only one and
    has the default name: there is nothing to tell apart."""
    if warp_name and (multi or warp_name != DEFAULT_WARP):
        return f"{node} / {warp_name}"
    return node


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Message building blocks: every operator-facing message is a card, a title
# line ("icon <b>name</b> · what") followed by detail rows drawn as a tree.
# Row values are markup built by the caller (escaped where they carry data).
# ---------------------------------------------------------------------------


# Names for the countries seen in practice. Any other valid ISO code still gets
# its flag and its code; add names with COUNTRY_NAMES="XK=Kosovo,AQ=Antarctica".
_COUNTRY_NAMES = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AL": "Albania", "AM": "Armenia",
    "AR": "Argentina", "AT": "Austria", "AU": "Australia", "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina", "BD": "Bangladesh", "BE": "Belgium", "BG": "Bulgaria",
    "BH": "Bahrain", "BR": "Brazil", "BY": "Belarus", "CA": "Canada", "CH": "Switzerland",
    "CL": "Chile", "CN": "China", "CO": "Colombia", "CR": "Costa Rica", "CU": "Cuba",
    "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "DZ": "Algeria",
    "EE": "Estonia", "EG": "Egypt", "ES": "Spain", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "GE": "Georgia", "GR": "Greece", "HK": "Hong Kong",
    "HR": "Croatia", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel",
    "IN": "India", "IQ": "Iraq", "IR": "Iran", "IS": "Iceland", "IT": "Italy",
    "JP": "Japan", "KE": "Kenya", "KG": "Kyrgyzstan", "KP": "North Korea",
    "KR": "South Korea", "KW": "Kuwait", "KZ": "Kazakhstan", "LI": "Liechtenstein",
    "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MA": "Morocco",
    "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro", "MK": "North Macedonia",
    "MT": "Malta", "MX": "Mexico", "MY": "Malaysia", "NG": "Nigeria", "NL": "Netherlands",
    "NO": "Norway", "NZ": "New Zealand", "PE": "Peru", "PH": "Philippines",
    "PK": "Pakistan", "PL": "Poland", "PT": "Portugal", "QA": "Qatar", "RO": "Romania",
    "RS": "Serbia", "RU": "Russia", "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore",
    "SI": "Slovenia", "SK": "Slovakia", "SY": "Syria", "TH": "Thailand", "TR": "Turkey",
    "TW": "Taiwan", "UA": "Ukraine", "US": "United States", "UZ": "Uzbekistan",
    "VE": "Venezuela", "VN": "Vietnam", "ZA": "South Africa", "EU": "European Union",
}
# Codes that are not countries but do occur: Cloudflare reports Tor as T1.
_SPECIAL_PLACES = {"T1": ("🧅", "Tor")}
_COUNTRY_ALIASES = {"UK": "GB"}


def _extra_country_names(spec):
    names = {}
    for part in spec.replace(";", ",").split(","):
        code, sep, name = part.partition("=")
        code, name = code.strip().upper(), name.strip()
        if sep and name and len(code) == 2 and code.isascii() and code.isalpha():
            names[code] = name
    return names


COUNTRY_NAMES = {**_COUNTRY_NAMES, **_extra_country_names(env("COUNTRY_NAMES", ""))}


def _norm_cc(cc):
    if not isinstance(cc, str):
        return ""
    cc = cc.strip().upper()
    return _COUNTRY_ALIASES.get(cc, cc)


def flag(cc):
    """Flag emoji for a country code (any valid ISO 3166 code), or '❔'."""
    cc = _norm_cc(cc)
    if cc in _SPECIAL_PLACES:
        return _SPECIAL_PLACES[cc][0]
    if len(cc) == 2 and cc.isascii() and cc.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc)
    return "❔"


def place(cc, full=False):
    """'🇳🇱 NL', or '🇳🇱 Netherlands' with full=True when the name is known.
    Anything that is not a valid code is '❔ unknown', never echoed."""
    cc = _norm_cc(cc)
    f = flag(cc)
    if f == "❔":
        return "❔ unknown"
    if full:
        name = COUNTRY_NAMES.get(cc) or (_SPECIAL_PLACES.get(cc) or ("", ""))[1]
        if name:
            return f"{f} {name}"
    return f"{f} {cc}"


def tree(*rows):
    """Detail rows with branches: ('Google', '🇳🇱 NL') -> '├ Google: 🇳🇱 NL'."""
    rows = [r for r in rows if r]
    return [
        f"{'└' if i == len(rows) - 1 else '├'} {label}: {value}"
        for i, (label, value) in enumerate(rows)
    ]


def card(icon, name, what, *rows):
    """Title line plus detail rows. `name` and `what` are plain text."""
    return "\n".join([f"{icon} <b>{esc(name)}</b> · {esc(what)}", *tree(*rows)])


# ---------------------------------------------------------------------------
# Validation of data reported by nodes (untrusted: any holder of a node token)
# ---------------------------------------------------------------------------

# Same alphabet as NODE_NAME in single/bin/warp-bot.sh.
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_COUNTRY_RE = re.compile(r"^([A-Z]{2}|T1)$")  # T1: Cloudflare's code for Tor
_IP_RE = re.compile(r"^[0-9A-Fa-f:.]{1,45}$")


def clean_country(value):
    """Upper-case two-letter country code, or '' for anything else."""
    if not isinstance(value, str):
        return ""
    value = value.strip().upper()
    return value if _COUNTRY_RE.match(value) else ""


def clean_warps(raw):
    """Validate the per-WARP list of a heartbeat: at most MAX_WARPS_PER_NODE
    entries, valid unique names, checked fields. Anything else is dropped."""
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw[:MAX_WARPS_PER_NODE]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not NODE_NAME_RE.match(name) or name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "google_country": clean_country(item.get("google_country")) or None,
                "alive": item.get("alive") is True,
                "last_restart": clean_ts(item.get("last_restart")),
            }
        )
    return out


def sync_warps(conn, node, warps, t):
    """Make the stored WARPs of a node match the list its agent reports."""
    for w in warps:
        conn.execute(
            "INSERT INTO warps (node, name, google_country, warp_alive, last_restart, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(node, name) DO UPDATE SET google_country = excluded.google_country, "
            "warp_alive = excluded.warp_alive, last_restart = excluded.last_restart, "
            "updated_at = excluded.updated_at",
            (node, w["name"], w["google_country"], 1 if w["alive"] else 0, w["last_restart"], t),
        )
    names = [w["name"] for w in warps]
    marks = ",".join("?" * len(names))
    gone = [
        r["name"]
        for r in conn.execute(
            f"SELECT name FROM warps WHERE node = ? AND name NOT IN ({marks})", (node, *names)
        )
    ]
    for name in gone:  # a WARP removed from the node: forget it and its alerts
        conn.execute("DELETE FROM warps WHERE node = ? AND name = ?", (node, name))
        conn.execute("DELETE FROM meta WHERE key = ?", (f"warp_alert:{node}:{name}",))
        conn.execute("DELETE FROM meta WHERE key = ?", (f"retry_unknown:{node}:{name}",))


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
    previous = raw.get("previous_ip")
    warp = raw.get("warp")
    remaining = raw.get("cooldown_remaining")
    return {
        "warp": warp if isinstance(warp, str) and NODE_NAME_RE.match(warp) else "",
        "previous_ip": previous if isinstance(previous, str) and _IP_RE.match(previous) else "",
        # The agent refused a restart because its cooldown is active.
        "skipped": "cooldown" if raw.get("skipped") == "cooldown" else "",
        "cooldown_remaining": (
            remaining if type(remaining) is int and 0 <= remaining <= 86400 else 0
        ),
        "warp_up": raw.get("warp_up") is True,
        "still_ru": raw.get("still_ru") is True,
        "country": clean_country(raw.get("country")),
        "cloudflare_loc": clean_country(raw.get("cloudflare_loc")),
        "exit_ip": ip if isinstance(ip, str) and _IP_RE.match(ip) else "",
    }


def _format_restart_message(name, meta, kind, ok=True, output=""):
    """Build the Telegram card for a restart event.

    kind: 'restart #N' (ordered by the operator) or 'auto-restart'.
    """
    if meta.get("skipped") == "cooldown":
        left = int(meta.get("cooldown_remaining") or 0)
        return card(
            "⏳", name, f"{kind} skipped", ("Cooldown", f"{left}s left, try again then")
        )

    if not meta.get("warp_up"):
        if not ok:  # e.g. docker compose failed: there is no report to show
            # The agent's output starts with a header ("compose restart
            # failed:"); the cause is at the end.
            lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
            reason = lines[-1][:160] if lines else ""
            return card(
                "🔴", name, f"{kind} failed", ("Reason", esc(reason)) if reason else None
            )
        return card("🔴", name, f"{kind} · WARP did not come up")

    country = (meta.get("country") or "").strip().upper()
    if meta.get("still_ru") or country in BAD_COUNTRIES:
        icon, what = "🟡", f"{kind} · still {country or 'blocked'}"
    elif country:
        icon, what = "🟢", f"{kind} · done"
    else:
        icon, what = "🟡", f"{kind} · country unknown"

    rows = [("Google", place(country, full=True))]
    if meta.get("cloudflare_loc"):
        rows.append(("Cloudflare", place(meta["cloudflare_loc"], full=True)))
    ip, before = meta.get("exit_ip"), meta.get("previous_ip")
    if ip and before and ip != before:
        rows.append(("Exit IP", f"<code>{esc(before)}</code> → <code>{esc(ip)}</code>"))
    elif ip:
        same = " (unchanged ⚠️)" if before == ip else ""
        rows.append(("Exit IP", f"<code>{esc(ip)}</code>{same}"))
    return card(icon, name, what, *rows)


# ===========================================================================
# Node state helpers
# ===========================================================================


def _warp_count(conn, node):
    """How many WARPs the node reported (0: its agent reports no list)."""
    return conn.execute("SELECT COUNT(*) AS c FROM warps WHERE node = ?", (node,)).fetchone()["c"]


def _pending_restart(conn, node, target):
    """Is a restart for this target already waiting? A command for the whole
    node ('') overlaps with every WARP, and the other way round."""
    if target:
        row = conn.execute(
            "SELECT id FROM commands WHERE node = ? AND command = 'restart' "
            "AND result_at IS NULL AND target IN (?, '') LIMIT 1",
            (node, target),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM commands WHERE node = ? AND command = 'restart' "
            "AND result_at IS NULL LIMIT 1",
            (node,),
        ).fetchone()
    return row is not None


def _retry_key(node, warp):
    return f"retry_unknown:{node}:{warp}" if warp else f"retry_unknown:{node}"


def _apply_meta_to_node(name, meta, warp=""):
    """Update the stored country and last_restart from a restart report.

    `warp` names the WARP the report is about. The node's own columns hold the
    legacy single-WARP summary and are only touched for a node with at most
    one WARP; with several, the agent's next heartbeat (sent right after every
    restart) refreshes them.
    """
    country = (meta.get("country") or "").strip()
    if not country or country == "?":
        return
    conn = db_connect()
    try:
        if warp:
            conn.execute(
                "UPDATE warps SET google_country = ?, last_restart = ?, warp_alive = 1, "
                "updated_at = ? WHERE node = ? AND name = ?",
                (country, now(), now(), name, warp),
            )
        if _warp_count(conn, name) <= 1:
            conn.execute(
                "UPDATE nodes SET google_country = ?, last_restart = ? WHERE name = ?",
                (country, now(), name),
            )
        conn.execute("DELETE FROM meta WHERE key = ?", (_retry_key(name, warp),))
    finally:
        conn.close()


def _schedule_unknown_retry(name, warp=""):
    """Queue a delayed restart if the last one ended with country == '?'."""
    if RETRY_UNKNOWN_LIMIT <= 0:
        return
    key = _retry_key(name, warp)
    conn = db_connect()
    try:
        if _pending_restart(conn, name, warp):
            return
        label = warp_label(name, warp, _warp_count(conn, name) > 1)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        count = int(row["value"]) if row else 0

        if count >= RETRY_UNKNOWN_LIMIT:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            log("WARN", f"{label}: gave up after {RETRY_UNKNOWN_LIMIT} retries")
            tg_send(
                card(
                    "⚠️",
                    label,
                    "giving up",
                    (
                        "Google country",
                        f"still unknown after {RETRY_UNKNOWN_LIMIT} restarts",
                    ),
                )
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
            "INSERT INTO commands (node, command, created_at, not_before, target) "
            "VALUES (?, 'restart', ?, ?, ?)",
            (name, t, t + RETRY_UNKNOWN_DELAY, warp),
        )
    finally:
        conn.close()
    log("INFO", f"{label}: queued retry #{count} in {RETRY_UNKNOWN_DELAY}s")
    tg_send(
        card(
            "🔁",
            label,
            "country still unknown",
            ("Retry", f"#{count}/{RETRY_UNKNOWN_LIMIT} in {fmt_dur(RETRY_UNKNOWN_DELAY)}"),
        )
    )


# ===========================================================================
# Command handlers
# ===========================================================================

HELP_TEXT = (
    "<b>warp-coordinator</b>\n\n"
    "/nodes              — list nodes\n"
    "/status &lt;node&gt;     — details for one node\n"
    "/restart &lt;node&gt;    — queue restart (all WARPs of the node)\n"
    "/restart &lt;node&gt; &lt;warp&gt; — restart one WARP\n"
    "/restart all        — queue restart for every node\n"
    "/logs &lt;node&gt;       — last command output\n"
    "/health             — coordinator health\n"
    "/help               — this message"
)


def _warp_icon(w):
    """🔴 down, 🟡 in a country Google refuses, 🟢 fine (for a fresh report)."""
    if not w["warp_alive"]:
        return "🔴"
    return "🟡" if (w["google_country"] or "").upper() in BAD_COUNTRIES else "🟢"


def _warp_detail(w, t, full=False):
    if not w["warp_alive"]:
        return "WARP down"
    country = (w["google_country"] or "").upper()
    detail = place(country, full=full)
    if country in BAD_COUNTRIES and w["last_restart"]:
        detail += f" · restarted {fmt_dur(t - w['last_restart'])} ago"
    return detail


def _worst_icon(icons):
    return "🔴" if "🔴" in icons else ("🟡" if "🟡" in icons else "🟢")


def cmd_nodes():
    conn = db_connect()
    try:
        rows = conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()
        data = [(r, node_warps(r, conn)) for r in rows]
    finally:
        conn.close()
    if not data:
        tg_send("🛰 <b>Nodes</b>\n\nNo nodes registered.")
        return
    t = now()
    lines = []
    for r, warps in data:
        state = node_state(r, t, warps)
        name = r["name"]
        if state == "never":
            lines.append(f"⚫ <code>{esc(name)}</code> · never connected")
        elif state == "stale":
            age = fmt_dur(t - r["last_heartbeat"])
            lines.append(f"⚫ <code>{esc(name)}</code> · silent {age}")
        elif len(warps) == 1:
            w = warps[0]
            label = warp_label(name, w["name"], False)
            lines.append(f"{_warp_icon(w)} <code>{esc(label)}</code> · {_warp_detail(w, t)}")
        else:
            icons = [_warp_icon(w) for w in warps]
            lines.append(f"{_worst_icon(icons)} <code>{esc(name)}</code>")
            for k, w in enumerate(warps):
                branch = "└" if k == len(warps) - 1 else "├"
                lines.append(f"{branch} {icons[k]} {esc(w['name'])} · {_warp_detail(w, t)}")
    tg_send("🛰 <b>Nodes</b>\n\n" + "\n".join(lines))


def cmd_status(name):
    conn = db_connect()
    try:
        r = conn.execute("SELECT * FROM nodes WHERE name = ?", (name,)).fetchone()
        warps = node_warps(r, conn) if r else []
    finally:
        conn.close()
    if not r:
        tg_send(f"❓ Unknown node <code>{esc(name)}</code>")
        return
    t = now()
    state = node_state(r, t, warps)
    multi = len(warps) > 1
    last_restart = max(w["last_restart"] or 0 for w in warps)
    restart = f"{fmt_dur(t - last_restart)} ago" if last_restart else "never"
    heartbeat = (
        f"{fmt_dur(t - r['last_heartbeat'])} ago" if r["last_heartbeat"] else "never"
    )
    title = name if multi else warp_label(name, warps[0]["name"], False)

    if state in ("never", "stale"):
        # No fresh report: do not present the last one as the current state.
        icon, what = "⚫", "never connected" if state == "never" else "STALE"
        if multi:
            rows = [
                (
                    w["name"],
                    f"unknown (last report: {place(w['google_country'], full=True)}, "
                    f"{'up' if w['warp_alive'] else 'down'})",
                )
                for w in warps
            ]
        else:
            w = warps[0]
            rows = [
                ("Google", f"unknown (last report: {place(w['google_country'], full=True)})"),
                ("WARP", f"unknown (last report: {'up' if w['warp_alive'] else 'down'})"),
            ]
    else:
        icons = [_warp_icon(w) for w in warps]
        icon = _worst_icon(icons)
        what = "WARP down" if state == "warp_down" else "status"
        if multi:
            rows = [
                (w["name"], f"{icons[k]} {_warp_detail(w, t, full=True)}")
                for k, w in enumerate(warps)
            ]
        else:
            w = warps[0]
            rows = [
                ("Google", place(w["google_country"], full=True)),
                ("WARP", "up" if w["warp_alive"] else "down"),
            ]
    tg_send(card(icon, title, what, *rows, ("Heartbeat", heartbeat), ("Last restart", restart)))


def _pick_warps(warps, wanted):
    """The WARPs a restart is for: the one called `wanted` (any case), or all.
    Returns (chosen, found); `chosen` holds (target, name) pairs, where target
    is what is stored on the command ('' for the implicit WARP of a node whose
    agent reports no list)."""
    implicit = len(warps) == 1 and warps[0].get("implicit")
    chosen = warps
    if wanted:
        chosen = [w for w in warps if w["name"].lower() == wanted.lower()]
        if not chosen:
            return [], False
    return [("" if implicit else w["name"], w["name"]) for w in chosen], True


def cmd_restart(name, wanted=None):
    expire_commands()  # an old command must not block a new one
    conn = db_connect()
    try:
        node = conn.execute("SELECT * FROM nodes WHERE name = ?", (name,)).fetchone()
        if not node:
            tg_send(f"❓ Unknown node <code>{esc(name)}</code>")
            return
        warps = node_warps(node, conn)
        chosen, found = _pick_warps(warps, wanted)
        if not found:
            known = ", ".join(w["name"] for w in warps)
            tg_send(
                f"❓ Unknown WARP <code>{esc(wanted)}</code> on <code>{esc(name)}</code>\n"
                f"Known: <code>{esc(known)}</code>"
            )
            return
        t = now()
        queued, pending = [], []
        for target, wname in chosen:
            if _pending_restart(conn, name, target):
                pending.append(wname)
                continue
            cur = conn.execute(
                "INSERT INTO commands (node, command, created_at, target) "
                "VALUES (?, 'restart', ?, ?)",
                (name, t, target),
            )
            queued.append((wname, cur.lastrowid))
        state = node_state(node, t, warps)
    finally:
        conn.close()

    multi = len(warps) > 1
    for wname, cid in queued:
        log("INFO", f"queued restart #{cid} for {warp_label(name, wname, multi)}")
    warning = None
    if state in ("never", "stale"):
        warning = (
            "⚠️ Warning",
            f"node is not reporting; expires in {fmt_dur(COMMAND_TTL)} if not picked up",
        )

    if len(chosen) == 1:
        label = warp_label(name, chosen[0][1], multi)
        if pending:
            tg_send(card("⏳", label, "restart already pending"))
        else:
            tg_send(card("⏳", label, f"restart #{queued[0][1]} queued", warning))
        return
    rows = [(wname, f"#{cid} queued") for wname, cid in queued]
    rows += [(wname, "already pending") for wname in pending]
    tg_send(card("⏳", name, "restart queued" if queued else "nothing queued", *rows, warning))


def cmd_restart_all():
    expire_commands()
    conn = db_connect()
    try:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
        if not rows:
            tg_send("No nodes registered.")
            return
        t = now()
        commands, nodes_hit, skipped = 0, set(), []
        for r in rows:
            warps = node_warps(r, conn)
            if node_state(r, t, warps) in ("never", "stale"):
                skipped.append((r["name"], "not reporting"))
                continue
            multi = len(warps) > 1
            chosen, _ = _pick_warps(warps, None)
            for target, wname in chosen:
                if _pending_restart(conn, r["name"], target):
                    skipped.append((warp_label(r["name"], wname, multi), "already pending"))
                    continue
                conn.execute(
                    "INSERT INTO commands (node, command, created_at, target) "
                    "VALUES (?, 'restart', ?, ?)",
                    (r["name"], t, target),
                )
                commands += 1
                nodes_hit.add(r["name"])
    finally:
        conn.close()
    log("INFO", f"queued {commands} restart(s) on {len(nodes_hit)} node(s), skipped {len(skipped)}")
    n = len(nodes_hit)
    text = f"🔄 <b>Restart queued</b> · {n} node{'' if n == 1 else 's'}"
    if commands != n:
        text += f", {commands} WARPs"
    if skipped:
        text += "\n⏭ Skipped:\n" + "\n".join(
            tree(*[(f"<code>{esc(n)}</code>", why) for n, why in skipped])
        )
    tg_send(text)


def cmd_logs(name):
    conn = db_connect()
    try:
        node = conn.execute("SELECT name FROM nodes WHERE name = ?", (name,)).fetchone()
        if not node:
            tg_send(f"❓ Unknown node <code>{esc(name)}</code>")
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
        tg_send(f"📭 <b>{esc(name)}</b> · no command history yet")
        return
    icon = "✅" if r["ok"] else "❌"
    body = esc(r["output"] or "")[:3000]
    tg_send(
        f"{icon} <b>{esc(name)}</b> · cmd#{r['id']} {esc(r['command'])}\n<pre>{body}</pre>"
    )


def cmd_health():
    with _state_lock:
        s = dict(_state)
    ok = s.get("bot_alive", False)
    icon = "🟢" if ok else "🔴"
    age = now() - s.get("last_poll_ts", 0)
    tg_send(
        card(
            icon,
            "Coordinator",
            "healthy" if ok else "bot loop stalled",
            ("Last poll", f"{fmt_dur(age)} ago"),
            ("Offset", s.get("offset", 0)),
        )
    )


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
            tg_send("Usage: /restart &lt;node&gt; [warp] | /restart all")
        elif args[0] == "all":
            cmd_restart_all()
        else:
            cmd_restart(args[0], args[1] if len(args) > 1 else None)
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


def _node_alert_text(name, row, state, t):
    if state == "stale":
        return card(
            "⚫",
            name,
            "stopped reporting",
            ("Last heartbeat", f"{fmt_dur(t - row['last_heartbeat'])} ago"),
            ("Likely", "the node, its agent or its network is down"),
        )
    return card("🟢", name, "reporting again")


def _warp_alert_text(label, state):
    if state == "down":
        return card("🔴", label, "WARP is down", ("Status", "node is up, proxy is not"))
    return card("🟢", label, "WARP is back")


def node_monitor_tick(t=None):
    """One pass over all nodes. Sends one message per state change the
    operator has not been told about yet and returns how many were sent.

    A node is watched on two levels: silent or reporting (key
    node_alert:<node>), and, while it reports, each of its WARPs up or down
    (key warp_alert:<node>:<warp>).

    * What the operator was last told is persisted in meta, so a repeat is
      never sent, including after a coordinator restart.
    * It is only updated after Telegram accepted the message, so an alert
      that could not be delivered is retried on the next pass.
    * Silence is not judged until STALE_AFTER seconds after start: nodes need
      that long to heartbeat again after a coordinator restart.
    * A WARP must stay down for WARP_DOWN_ALERT_AFTER seconds, so the short
      outage of an ordinary restart does not page anyone.
    """
    global _monitor_started
    t = now() if t is None else t
    if _monitor_started is None:
        _monitor_started = t

    conn = db_connect()
    try:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
        data = [(r, node_warps(r, conn)) for r in rows]
    finally:
        conn.close()

    sent = 0

    def step(key, seen_key, state, delay, text):
        """Alert on a change of `state` (the first value is the quiet one)."""
        nonlocal sent
        seen = _node_seen.get(seen_key)
        if seen is None or seen[0] != state:
            seen = _node_seen[seen_key] = (state, t)
        if t - seen[1] < delay:
            return
        told = meta_get(key, "ok")
        if told == state:
            return
        if tg_send(text):
            meta_set(key, state)
            sent += 1
            log("WARN", f"alert sent: {key} {told} -> {state}")
        else:
            log("WARN", f"alert for {key} ({state}) not delivered; will retry")

    for r, warps in data:
        name = r["name"]
        state = node_state(r, t, warps)
        if state == "never":
            continue
        node_level = "stale" if state == "stale" else "ok"
        if node_level == "stale" and t - _monitor_started < STALE_AFTER:
            continue

        # Older versions kept "warp_down" under the node key.
        if meta_get(f"node_alert:{name}", "ok") == "warp_down":
            meta_set(f"warp_alert:{name}:{warps[0]['name']}", "down")
            meta_set(f"node_alert:{name}", "ok")

        step(
            f"node_alert:{name}",
            (name, None),
            node_level,
            0,
            _node_alert_text(name, r, node_level, t),
        )
        if node_level == "stale":
            continue  # a silent node says nothing about its WARPs
        for w in warps:
            wstate = "ok" if w["warp_alive"] else "down"
            label = warp_label(name, w["name"], len(warps) > 1)
            step(
                f"warp_alert:{name}:{w['name']}",
                (name, w["name"]),
                wstate,
                WARP_DOWN_ALERT_AFTER if wstate == "down" else 0,
                _warp_alert_text(label, wstate),
            )

    known = {(r["name"], None) for r, _ in data}
    known |= {(r["name"], w["name"]) for r, ws in data for w in ws}
    for key in [k for k in _node_seen if k not in known]:
        del _node_seen[key]
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
                    "\n".join(
                        [
                            "⌛ <b>Commands expired</b> · not picked up in time",
                            *tree(*[(f"<code>{esc(n)}</code>", f"#{i}") for i, n in expired]),
                        ]
                    )
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
                tg_send(f"❌ <b>{failed} command(s) failed</b> · lease expired")
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
        # Compare digests (as bytes: compare_digest rejects non-ASCII str).
        if not r or not secrets.compare_digest(
            r["token"].encode("utf-8"), token_digest(token).encode("utf-8")
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
            t = now()
            states = [node_state(r, t, node_warps(r, conn)) for r in node_rows]
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM commands WHERE result_at IS NULL"
            ).fetchone()["c"]
        finally:
            conn.close()
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
            warps = clean_warps(b.get("warps"))
            if warps:
                sync_warps(conn, name, warps, now())
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
                "SELECT id, command, target FROM commands "
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
            200,
            {
                "commands": [
                    {"id": r["id"], "cmd": r["command"], "warp": r["target"]} for r in rows
                ]
            },
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
            target = ""
            if recorded:
                row = conn.execute(
                    "SELECT target FROM commands WHERE id = ?", (cid,)
                ).fetchone()
                target = row["target"] if row else ""
                warp_name = target or meta["warp"]
                label = warp_label(name, warp_name, _warp_count(conn, name) > 1)
        finally:
            conn.close()

        if not recorded:
            log("INFO", f"ignored result for cmd#{cid} from {name}: not open")
            # 200 so the agent drops it from its pending queue instead of
            # resending the same payload forever.
            self.reply(200, {"ok": True, "ignored": True})
            return

        _apply_meta_to_node(name, meta, warp_name)

        country = (meta.get("country") or "").strip()
        if meta.get("skipped"):
            log(
                "INFO",
                f"result cmd#{cid} from {label}: skipped "
                f"({meta['skipped']}, {meta['cooldown_remaining']}s left)",
            )
        else:
            log(
                "INFO",
                f"result cmd#{cid} from {label}: ok={ok} "
                f"warp_up={meta.get('warp_up')} country={country or '?'}",
            )

        tg_send(_format_restart_message(label, meta, f"restart #{cid}", ok=ok, output=out))

        if ok and meta.get("warp_up") and (not country or country == "?"):
            _schedule_unknown_retry(name, target)

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
        wanted = b.get("warp")
        warp_name = (
            wanted if isinstance(wanted, str) and NODE_NAME_RE.match(wanted) else meta["warp"]
        )
        conn = db_connect()
        try:
            label = warp_label(name, warp_name, _warp_count(conn, name) > 1)
        finally:
            conn.close()

        _apply_meta_to_node(name, meta, warp_name)

        if reason == "auto" and isinstance(raw_meta, dict) and raw_meta:
            tg_send(_format_restart_message(label, meta, "auto-restart"))
        elif text:
            tg_send(f"💬 <b>{esc(label)}</b>\n{esc(text)}")

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
        existed = conn.execute(
            "SELECT 1 FROM nodes WHERE name = ?", (name,)
        ).fetchone()
        conn.execute(
            "INSERT INTO nodes (name, token, registered_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET token = excluded.token",
            (name, token_digest(token), now()),
        )
    finally:
        conn.close()
    if existed:
        print(
            f"note: node {name} already existed; its previous token no longer works",
            file=sys.stderr,
        )
    print(f"NODE_NAME={name}")
    print(f"NODE_TOKEN={token}")


def cli_remove_node(name):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM nodes WHERE name = ?", (name,))
        conn.execute("DELETE FROM commands WHERE node = ?", (name,))
        conn.execute("DELETE FROM warps WHERE node = ?", (name,))
        for prefix in ("retry_unknown", "node_alert"):
            conn.execute("DELETE FROM meta WHERE key = ?", (f"{prefix}:{name}",))
        # Per-WARP keys ("<prefix>:<node>:<warp>"). Compared as text, not with
        # LIKE: '_' in a node name would be a wildcard there.
        for prefix in ("retry_unknown", "warp_alert"):
            head = f"{prefix}:{name}:"
            conn.execute(
                "DELETE FROM meta WHERE substr(key, 1, ?) = ?", (len(head), head)
            )
    finally:
        conn.close()
    print(f"Removed {name}")


def cli_list_nodes():
    conn = db_connect()
    try:
        rows = conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()
        data = [(r, node_warps(r, conn)) for r in rows]
    finally:
        conn.close()
    if not data:
        print("(no nodes)")
        return
    t = now()

    def fields(w):
        return (w["google_country"] or "?", "yes" if w["warp_alive"] else "no")

    for r, warps in data:
        state = node_state(r, t, warps)
        label = {"never": "NEVER", "stale": "STALE", "warp_down": "WARP-DOWN"}.get(state, "OK")
        multi = len(warps) > 1
        if state == "never":
            detail = "hb=never"
        else:
            age = fmt_dur(t - r["last_heartbeat"])
            if state == "stale":
                last = ""
                if not multi:
                    country, alive = fields(warps[0])
                    last = f" (last report: country={country} alive={alive})"
                detail = f"hb={age} ago{last}"
            elif multi:
                detail = f"hb={age}"
            else:
                country, alive = fields(warps[0])
                detail = f"hb={age} country={country} alive={alive}"
        print(f"{r['name']:30s} {label:9s} {detail}")
        if multi:
            for w in warps:
                country, alive = fields(w)
                prefix = "last report: " if state in ("never", "stale") else ""
                print(f"  {w['name']:<28s} {prefix}country={country} alive={alive}")


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
