"""Tests for fleet/coordinator/coordinator.py.

The coordinator is exercised through its real HTTP handler on an ephemeral
port, with a temporary SQLite database, a controllable clock and Telegram
sending stubbed out.

Run with:  python3 -m unittest discover -s fleet/tests -v
"""

import contextlib
import importlib.util
import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]

os.environ.setdefault("TG_TOKEN", "test-token")
os.environ.setdefault("TG_CHAT_ID", "1")

_spec = importlib.util.spec_from_file_location(
    "warp_coordinator", ROOT / "fleet" / "coordinator" / "coordinator.py"
)
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class CoordinatorTestCase(unittest.TestCase):
    NODE = "n1"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = [1_000_000]
        self.sent = []
        self.db_path = Path(tmp.name) / "state" / "coordinator.db"

        for name, new in {
            "DB_PATH": self.db_path,
            "TG_CHAT_ID": 1,
            "now": lambda: self.clock[0],
            "tg_send": lambda text: self.sent.append(text),
        }.items():
            patcher = mock.patch.object(C, name, new)
            patcher.start()
            self.addCleanup(patcher.stop)

        C.db_init()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            C.cli_add_node(self.NODE)
        self.token = next(
            line.split("=", 1)[1]
            for line in out.getvalue().splitlines()
            if line.startswith("NODE_TOKEN=")
        )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    # -- helpers ------------------------------------------------------------

    def auth_headers(self):
        return {
            "X-Node": self.NODE,
            "X-Token": self.token,
            "Content-Type": "application/json",
        }

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=self.auth_headers() if headers is None else headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def sql(self, query, *args):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def queue_restart(self):
        self.sql(
            "INSERT INTO commands (node, command, created_at) VALUES (?, 'restart', ?)",
            self.NODE,
            self.clock[0],
        )

    def pending_restarts(self):
        return self.sql(
            "SELECT * FROM commands WHERE node = ? AND command = 'restart' "
            "AND result_at IS NULL",
            self.NODE,
        )

    def deliver(self):
        status, body = self.call("GET", "/commands")
        self.assertEqual(status, 200)
        return body["commands"]

    def post_result(self, cid, ok=True, country="", warp_up=True):
        return self.call(
            "POST",
            "/result",
            {
                "id": cid,
                "ok": ok,
                "output": "",
                "meta": {"warp_up": warp_up, "country": country},
            },
        )


class UnknownCountryRetryTest(CoordinatorTestCase):
    """A restart that ends with an unknown Google country used to queue the
    next restart immediately, up to 10 times in a row, with no delay."""

    def patch_limits(self, limit, delay=330):
        for name, value in {
            "RETRY_UNKNOWN_LIMIT": limit,
            "RETRY_UNKNOWN_DELAY": delay,
        }.items():
            patcher = mock.patch.object(C, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_retry_is_delayed_not_immediate(self):
        self.patch_limits(limit=2, delay=330)
        self.queue_restart()
        (cmd,) = self.deliver()
        self.post_result(cmd["id"], country="")

        self.assertEqual(len(self.pending_restarts()), 1)
        self.assertEqual(self.deliver(), [], "retry delivered with no delay")
        self.clock[0] += 329
        self.assertEqual(self.deliver(), [])
        self.clock[0] += 1
        self.assertEqual(len(self.deliver()), 1)

    def test_retry_chain_is_bounded_and_spaced(self):
        self.patch_limits(limit=2, delay=330)
        self.queue_restart()
        delivered_at = []
        for _ in range(20):
            cmds = self.deliver()
            if not cmds:
                self.clock[0] += 330
                cmds = self.deliver()
                if not cmds:
                    break
            for cmd in cmds:
                delivered_at.append(self.clock[0])
                self.post_result(cmd["id"], country="")

        self.assertEqual(len(delivered_at), 1 + 2)  # the original + 2 retries
        gaps = [b - a for a, b in zip(delivered_at, delivered_at[1:])]
        self.assertTrue(all(g >= 330 for g in gaps), gaps)
        self.assertTrue(any("giving up" in m for m in self.sent))

    def test_zero_limit_disables_retries(self):
        self.patch_limits(limit=0)
        self.queue_restart()
        (cmd,) = self.deliver()
        self.post_result(cmd["id"], country="")
        self.assertEqual(self.pending_restarts(), [])

    def test_no_second_retry_while_one_is_pending(self):
        self.patch_limits(limit=5)
        C._schedule_unknown_retry(self.NODE)
        C._schedule_unknown_retry(self.NODE)
        self.assertEqual(len(self.pending_restarts()), 1)

    def test_known_country_resets_the_counter(self):
        self.patch_limits(limit=2)
        self.queue_restart()
        (cmd,) = self.deliver()
        self.post_result(cmd["id"], country="")
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}"), "1")

        self.clock[0] += 330
        (retry,) = self.deliver()
        self.post_result(retry["id"], country="DE")
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}"), "")
        self.assertEqual(self.pending_restarts(), [])


class ResultIdempotencyTest(CoordinatorTestCase):
    """The agent re-sends a result when the response was lost. The second
    copy used to send another Telegram card and schedule another retry."""

    def setUp(self):
        super().setUp()
        for name, value in {"RETRY_UNKNOWN_LIMIT": 5, "RETRY_UNKNOWN_DELAY": 330}.items():
            patcher = mock.patch.object(C, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_command(self):
        self.queue_restart()
        (cmd,) = self.deliver()
        return cmd["id"]

    def test_first_result_is_recorded_and_announced(self):
        cid = self.open_command()
        status, _ = self.post_result(cid, country="DE")
        self.assertEqual(status, 200)
        (row,) = self.sql("SELECT ok, result_at FROM commands WHERE id = ?", cid)
        self.assertEqual(row["ok"], 1)
        self.assertIsNotNone(row["result_at"])
        self.assertEqual(len(self.sent), 1)

    def test_repeated_result_has_no_side_effects(self):
        cid = self.open_command()
        self.post_result(cid, country="")  # unknown -> schedules one retry
        sent_after_first = len(self.sent)
        self.assertEqual(len(self.pending_restarts()), 1)

        status, body = self.post_result(cid, country="")  # resend
        self.assertEqual(len(self.sent), sent_after_first, "duplicate notification")
        self.assertEqual(len(self.pending_restarts()), 1, "duplicate retry queued")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ignored"))

    def test_result_for_another_nodes_command_is_ignored(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            C.cli_add_node("n2")
        self.sql(
            "INSERT INTO commands (node, command, created_at) VALUES ('n2', 'restart', ?)",
            self.clock[0],
        )
        (other,) = self.sql("SELECT id FROM commands WHERE node = 'n2'")
        status, body = self.post_result(other["id"], country="DE")
        self.assertEqual(self.sent, [], "notified about a foreign command")
        (row,) = self.sql("SELECT result_at FROM commands WHERE id = ?", other["id"])
        self.assertIsNone(row["result_at"])
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ignored"))

    def test_late_result_replaces_a_watchdog_timeout_once(self):
        cid = self.open_command()
        self.sql(
            "UPDATE commands SET ok = 0, output = ?, result_at = ? WHERE id = ?",
            C.LEASE_EXPIRED_OUTPUT,
            self.clock[0],
            cid,
        )
        self.post_result(cid, country="DE")
        (row,) = self.sql("SELECT ok, output FROM commands WHERE id = ?", cid)
        self.assertEqual(row["ok"], 1)
        self.assertNotEqual(row["output"], C.LEASE_EXPIRED_OUTPUT)
        self.assertEqual(len(self.sent), 1)

        self.post_result(cid, country="DE")  # and now it is closed for good
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
