"""Tests for fleet/coordinator/coordinator.py.

The coordinator is exercised through its real HTTP handler on an ephemeral
port, with a temporary SQLite database, a controllable clock and Telegram
sending stubbed out.

Run with:  python3 -m unittest discover -s fleet/tests -v
"""

import contextlib
import http.client
import importlib.util
import io
import json
import logging
import os
import socket
import stat
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
        self.tg_ok = True  # set False to simulate Telegram being unreachable

        def fake_tg_send(text):
            if not self.tg_ok:
                return False
            self.sent.append(text)
            return True

        self.db_path = Path(tmp.name) / "state" / "coordinator.db"

        for name, new in {
            "DB_PATH": self.db_path,
            "TG_CHAT_ID": 1,
            "now": lambda: self.clock[0],
            "tg_send": fake_tg_send,
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


class NodeInputValidationTest(CoordinatorTestCase):
    """Reports come from nodes, i.e. from anyone holding a node token. They
    end up in Telegram messages sent with parse_mode=HTML."""

    EVIL = '<a href="https://evil.example/login">re-auth</a>'

    def heartbeat(self, **fields):
        return self.call("POST", "/heartbeat", {"warp_alive": True, **fields})

    def stored(self, column):
        (row,) = self.sql(f"SELECT {column} FROM nodes WHERE name = ?", self.NODE)
        return row[column]

    def test_heartbeat_country_is_normalised(self):
        self.heartbeat(google_country="de")
        self.assertEqual(self.stored("google_country"), "DE")

    def test_heartbeat_with_markup_country_is_not_stored(self):
        status, _ = self.heartbeat(google_country=self.EVIL)
        self.assertEqual(status, 200)
        self.assertIsNone(self.stored("google_country"))
        C.cmd_nodes()
        self.assertNotIn("evil.example", self.sent[-1])

    def test_heartbeat_with_bad_timestamp_does_not_fail(self):
        for bad in ("abc", [1], {"a": 1}, -5, self.clock[0] + 10**9):
            status, _ = self.heartbeat(last_restart=bad)
            self.assertEqual(status, 200, bad)
            self.assertEqual(self.stored("last_restart"), 0, bad)

    def test_result_meta_is_whitelisted(self):
        self.queue_restart()
        (cmd,) = self.deliver()
        status, _ = self.call(
            "POST",
            "/result",
            {
                "id": cmd["id"],
                "ok": True,
                "output": "",
                "meta": {
                    "warp_up": True,
                    "country": self.EVIL,
                    "cloudflare_loc": self.EVIL,
                    "exit_ip": self.EVIL,
                    "surprise": self.EVIL,
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.sent)
        for message in self.sent:
            self.assertNotIn("evil.example", message)
        self.assertIsNone(self.stored("google_country"))

    def test_non_dict_meta_and_non_integer_id_do_not_fail(self):
        for body in (
            {"id": 1, "ok": True, "meta": "oops"},
            {"id": 1, "ok": True, "meta": [1, 2]},
            {"id": "1; DROP TABLE nodes", "ok": True, "meta": {}},
            {"id": [1], "ok": True},
        ):
            status, _ = self.call("POST", "/result", body)
            self.assertEqual(status, 200, body)
        self.assertEqual(len(self.sql("SELECT name FROM nodes")), 1)

    def test_notify_meta_is_whitelisted(self):
        status, _ = self.call(
            "POST",
            "/notify",
            {"reason": "auto", "meta": {"warp_up": True, "country": self.EVIL}},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("evil.example", " ".join(self.sent))

    def test_values_already_in_the_database_are_escaped_on_output(self):
        self.sql(
            "UPDATE nodes SET last_heartbeat = ?, warp_alive = 1, "
            "google_country = ? WHERE name = ?",
            self.clock[0],
            "<i>x",
            self.NODE,
        )
        self.sql(
            "INSERT INTO nodes (name, token, registered_at, last_heartbeat, warp_alive) "
            "VALUES ('<b>legacy', 'tok', 0, 0, 0)"
        )
        C.cmd_nodes()
        self.assertNotIn("<b>legacy", self.sent[-1])
        self.assertNotIn("<i>x", self.sent[-1])
        self.assertIn("&lt;b&gt;legacy", self.sent[-1])
        C.cmd_status(self.NODE)
        self.assertNotIn("<i>x", self.sent[-1])

    def test_add_node_rejects_names_the_bot_cannot_address(self):
        for bad in ("", "has space", "<b>", "a/b", "x" * 65, "caf\u00e9"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                with contextlib.redirect_stdout(io.StringIO()):
                    C.cli_add_node(bad)
        with contextlib.redirect_stdout(io.StringIO()):
            C.cli_add_node("fra-1.node_2")
        self.assertEqual(len(self.sql("SELECT name FROM nodes")), 2)


class HttpHardeningTest(CoordinatorTestCase):
    def request(self, method, path, headers, send_body=b"", timeout=3):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=timeout)
        self.addCleanup(conn.close)
        conn.putrequest(method, path)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders(send_body)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_default_socket_timeout_is_set(self):
        self.assertIsNotNone(C.Handler.timeout)
        self.assertLessEqual(C.Handler.timeout, 60)

    def test_stalled_client_is_disconnected(self):
        with mock.patch.object(C.Handler, "timeout", 0.5):
            sock = socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=5)
            self.addCleanup(sock.close)
            sock.sendall(b"POST /heartbeat HTTP/1.1\r\nHost: x\r\n")  # never finished
            self.assertEqual(sock.recv(1024), b"")  # server closed the connection

    def test_oversized_body_is_rejected_without_reading_it(self):
        headers = dict(self.auth_headers(), **{"Content-Length": str(10**9)})
        status, _ = self.request("POST", "/heartbeat", headers)
        self.assertEqual(status, 413)

    def test_malformed_content_length_is_a_client_error(self):
        for bad in ("abc", "-1", "1.5"):
            headers = dict(self.auth_headers(), **{"Content-Length": bad})
            status, _ = self.request("POST", "/heartbeat", headers)
            self.assertEqual(status, 400, bad)

    def test_unauthenticated_request_never_reaches_the_body(self):
        headers = {"X-Node": self.NODE, "X-Token": "wrong", "Content-Length": str(10**9)}
        status, _ = self.request("POST", "/heartbeat", headers)
        self.assertEqual(status, 401)

    def test_non_ascii_token_is_unauthorized_not_a_server_error(self):
        headers = {"X-Node": self.NODE, "X-Token": "t\u00e9st"}
        status, body = self.request("GET", "/commands", headers)
        self.assertEqual(status, 401)
        self.assertNotIn(b"non-ASCII", body)

    def test_internal_errors_do_not_leak_details(self):
        with mock.patch.object(
            C.Handler, "r_health", side_effect=RuntimeError("secret /var/lib/x")
        ):
            status, body = self.request("GET", "/health", {})
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)

    def test_normal_requests_still_work(self):
        status, _ = self.call("POST", "/heartbeat", {"warp_alive": True, "google_country": "DE"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/commands")[1], {"commands": []})


class StaleNodeDisplayTest(CoordinatorTestCase):
    """A node that stopped heartbeating used to keep showing its last report
    (e.g. alive=yes) in `list-nodes` and /status."""

    def row(self):
        return self.sql("SELECT * FROM nodes WHERE name = ?", self.NODE)[0]

    def beat(self, **fields):
        self.call("POST", "/heartbeat", {"warp_alive": True, "google_country": "DE", **fields})

    def list_nodes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            C.cli_list_nodes()
        return out.getvalue()

    def test_node_state_classification(self):
        self.assertEqual(C.node_state(self.row(), self.clock[0]), "never")
        self.beat()
        self.assertEqual(C.node_state(self.row(), self.clock[0]), "ok")
        self.beat(warp_alive=False)
        self.assertEqual(C.node_state(self.row(), self.clock[0]), "warp_down")
        self.clock[0] += C.STALE_AFTER + 1
        self.assertEqual(C.node_state(self.row(), self.clock[0]), "stale")

    def test_list_nodes_does_not_report_a_silent_node_as_alive(self):
        self.beat()
        self.assertIn(" OK ", self.list_nodes())
        self.clock[0] += 18 * 86400
        line = self.list_nodes().splitlines()[0]
        self.assertIn("STALE", line)
        self.assertIn("18d ago", line)
        self.assertNotIn(" OK ", line)
        # the old report may be shown, but only labelled as such
        self.assertIn("last report", line)

    def test_status_does_not_report_a_silent_node_as_alive(self):
        self.beat()
        self.clock[0] += 18 * 86400
        C.cmd_status(self.NODE)
        message = self.sent[-1]
        self.assertIn("STALE", message)
        self.assertIn("WARP: unknown", message)
        self.assertIn("Google: unknown", message)
        self.assertNotIn("WARP: up", message)

    def test_status_of_a_fresh_node_is_unchanged(self):
        self.beat()
        C.cmd_status(self.NODE)
        self.assertIn("WARP: up", self.sent[-1])
        self.assertIn("Google: 🇩🇪 Germany", self.sent[-1])

    def test_node_that_never_connected_is_labelled(self):
        self.assertIn("NEVER", self.list_nodes())
        C.cmd_nodes()
        self.assertIn("never connected", self.sent[-1])


class NodeAlertsTest(CoordinatorTestCase):
    """Nobody used to be told when a node went silent: STALE_AFTER only
    changed how /nodes drew the row, and a node could be gone for 18 days
    without a single message."""

    def setUp(self):
        super().setUp()
        for name, value in {"_node_seen": {}, "_monitor_started": None}.items():
            patcher = mock.patch.object(C, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def beat(self, warp_alive=True):
        self.call("POST", "/heartbeat", {"warp_alive": warp_alive, "google_country": "DE"})

    def tick(self, advance=0):
        self.clock[0] += advance
        return C.node_monitor_tick()

    def test_silent_node_is_reported_once(self):
        self.beat()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(C.STALE_AFTER + 1), 1)
        self.assertIn("stopped reporting", self.sent[-1])
        for _ in range(50):  # for the next 18 days: no repeats
            self.assertEqual(self.tick(30 * 86400 // 50), 0)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_is_reported(self):
        self.beat()
        self.tick()
        self.tick(C.STALE_AFTER + 1)
        self.beat()
        self.assertEqual(self.tick(), 1)
        self.assertIn("reporting again", self.sent[-1])
        self.assertEqual(self.tick(30), 0)

    def test_no_verdict_on_silence_right_after_coordinator_start(self):
        self.beat()
        self.clock[0] += 5 * 86400  # the coordinator was down for days
        self.assertEqual(self.tick(), 0, "judged silence before nodes could report")
        self.assertEqual(self.tick(C.STALE_AFTER // 2), 0)
        self.assertEqual(self.tick(C.STALE_AFTER), 1)

    def test_warp_down_is_reported_only_if_it_persists(self):
        self.beat()
        self.tick()
        self.beat(warp_alive=False)
        self.assertEqual(self.tick(), 0)
        self.clock[0] += C.WARP_DOWN_ALERT_AFTER - 1
        self.beat(warp_alive=False)
        self.assertEqual(self.tick(), 0)
        self.clock[0] += 1
        self.beat(warp_alive=False)
        self.assertEqual(self.tick(), 1)
        self.assertIn("WARP is down", self.sent[-1])
        self.beat(warp_alive=True)
        self.assertEqual(self.tick(), 1)
        self.assertIn("WARP is back", self.sent[-1])

    def test_short_warp_outage_does_not_alert(self):
        self.beat()
        self.tick()
        self.beat(warp_alive=False)
        self.tick()
        self.beat(warp_alive=True)
        self.assertEqual(self.tick(30), 0)
        self.assertEqual(self.sent, [])

    def test_undelivered_alert_is_retried_until_it_goes_through(self):
        self.beat()
        self.tick()
        self.tg_ok = False
        self.assertEqual(self.tick(C.STALE_AFTER + 1), 0)
        self.assertEqual(self.tick(30), 0)
        self.assertEqual(C.meta_get(f"node_alert:{self.NODE}", "ok"), "ok")
        self.tg_ok = True
        self.assertEqual(self.tick(30), 1)
        self.assertEqual(self.tick(30), 0)

    def test_state_survives_a_coordinator_restart(self):
        self.beat()
        self.tick()
        self.tick(C.STALE_AFTER + 1)
        self.assertEqual(len(self.sent), 1)
        C._node_seen.clear()
        C._monitor_started = None  # what a fresh process looks like
        self.tick()
        self.tick(C.STALE_AFTER + 1)
        self.assertEqual(len(self.sent), 1, "re-alerted after restart")

    def test_node_that_never_connected_is_not_an_outage(self):
        self.tick()
        self.assertEqual(self.tick(10 * 86400), 0)

    def test_remove_node_forgets_alert_state(self):
        self.beat()
        self.tick()
        self.tick(C.STALE_AFTER + 1)
        with contextlib.redirect_stdout(io.StringIO()):
            C.cli_remove_node(self.NODE)
        self.assertEqual(C.meta_get(f"node_alert:{self.NODE}", "gone"), "gone")

    def test_health_endpoint_counts_problem_nodes(self):
        self.beat(warp_alive=False)
        status, body = self.call("GET", "/health", headers={})
        self.assertEqual((status, body["nodes_warp_down"], body["nodes_stale"]), (200, 1, 0))
        self.clock[0] += C.STALE_AFTER + 1
        _, body = self.call("GET", "/health", headers={})
        self.assertEqual((body["nodes_warp_down"], body["nodes_stale"]), (0, 1))


class CommandExpiryTest(CoordinatorTestCase):
    """A command queued for a node that never answers used to stay pending
    forever: /restart said "already pending", and the node ran the old
    command whenever it came back."""

    def beat(self):
        self.call("POST", "/heartbeat", {"warp_alive": True, "google_country": "DE"})

    def command_row(self, cid):
        return self.sql("SELECT * FROM commands WHERE id = ?", cid)[0]

    def test_command_the_node_never_fetched_expires(self):
        self.queue_restart()
        self.clock[0] += C.COMMAND_TTL - 1
        self.assertEqual(C.expire_commands(), [])
        self.clock[0] += 2
        self.assertEqual(C.expire_commands(), [(1, self.NODE)])
        row = self.command_row(1)
        self.assertEqual(row["ok"], 0)
        self.assertIn("expired", row["output"])
        self.assertEqual(C.expire_commands(), [], "expired twice")

    def test_expired_command_is_not_delivered_even_before_the_watchdog_runs(self):
        self.queue_restart()
        self.clock[0] += C.COMMAND_TTL + 1
        self.assertEqual(self.deliver(), [])

    def test_fresh_command_is_delivered(self):
        self.queue_restart()
        self.clock[0] += C.COMMAND_TTL - 5
        self.assertEqual(len(self.deliver()), 1)

    def test_command_in_flight_is_not_expired(self):
        self.queue_restart()
        self.assertEqual(len(self.deliver()), 1)  # delivered, result pending
        self.clock[0] += C.COMMAND_TTL + 1
        self.assertEqual(C.expire_commands(), [])

    def test_delayed_retry_is_not_expired_before_it_is_due(self):
        due = self.clock[0] + 330
        self.sql(
            "INSERT INTO commands (node, command, created_at, not_before) "
            "VALUES (?, 'restart', ?, ?)",
            self.NODE, self.clock[0], due,
        )
        self.clock[0] = due + 60
        self.assertEqual(C.expire_commands(), [])
        self.assertEqual(len(self.deliver()), 1)

    def test_restart_is_not_blocked_by_an_expired_command(self):
        self.beat()
        C.cmd_restart(self.NODE)
        self.assertIn("queued", self.sent[-1])
        self.clock[0] += 18 * 86400
        C.cmd_restart(self.NODE)
        self.assertIn("queued", self.sent[-1])
        self.assertNotIn("already pending", self.sent[-1])

    def test_restart_of_a_silent_node_warns(self):
        self.beat()
        self.clock[0] += C.STALE_AFTER + 1
        C.cmd_restart(self.NODE)
        self.assertIn("not reporting", self.sent[-1])
        self.assertIn("expires", self.sent[-1])

    def test_restart_all_skips_silent_nodes_and_nodes_with_a_pending_restart(self):
        for name in ("fresh", "busy", "silent"):
            with contextlib.redirect_stdout(io.StringIO()):
                C.cli_add_node(name)
        self.sql("UPDATE nodes SET last_heartbeat = ?, warp_alive = 1 "
                 "WHERE name IN ('fresh', 'busy', 'n1')", self.clock[0])
        self.sql("UPDATE nodes SET last_heartbeat = ? WHERE name = 'silent'",
                 self.clock[0] - 5 * 86400)
        self.sql("INSERT INTO commands (node, command, created_at) "
                 "VALUES ('busy', 'restart', ?)", self.clock[0])
        C.cmd_restart_all()
        queued = {r["node"] for r in self.sql(
            "SELECT node FROM commands WHERE result_at IS NULL")}
        self.assertEqual(queued, {"busy", "fresh", "n1"})
        message = self.sent[-1]
        self.assertIn("Restart queued</b> · 2 nodes", message)
        self.assertIn("<code>silent</code>: not reporting", message)
        self.assertIn("<code>busy</code>: already pending", message)

    def test_old_finished_commands_are_pruned(self):
        t = self.clock[0]
        old, recent = 40 * 86400, 5 * 86400
        for result_at in (t - old, t - recent, None):
            self.sql("INSERT INTO commands (node, command, created_at, result_at) "
                     "VALUES (?, 'restart', ?, ?)", self.NODE, t - old, result_at)
        self.assertEqual(C.prune_commands(), 1)
        self.assertEqual(len(self.sql("SELECT id FROM commands")), 2)


class DeadmanPingTest(CoordinatorTestCase):
    """The coordinator is itself a single point of silence: if it dies, or
    cannot reach Telegram, every alert stops at once. It therefore pings an
    external service that raises the alarm when the pings stop."""

    def setUp(self):
        super().setUp()
        self.pings = []
        pings = self.pings

        class Ping(C.BaseHTTPRequestHandler):
            def do_GET(self):
                pings.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, *a):
                pass

        ping_srv = ThreadingHTTPServer(("127.0.0.1", 0), Ping)
        threading.Thread(target=ping_srv.serve_forever, daemon=True).start()
        self.addCleanup(ping_srv.server_close)
        self.addCleanup(ping_srv.shutdown)
        self.url = f"http://127.0.0.1:{ping_srv.server_address[1]}/ping/abc"
        for name, value in {"DEADMAN_URL": self.url, "NODE_ALERTS": True}.items():
            patcher = mock.patch.object(C, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.set_state(bot_alive=True, monitor_ts=self.clock[0])

    def set_state(self, **kw):
        with C._state_lock:
            C._state.update(kw)
        self.addCleanup(lambda: C._state.clear())

    def test_pings_when_the_coordinator_is_healthy(self):
        self.assertTrue(C.deadman_ping())
        self.assertEqual(self.pings, ["/ping/abc"])

    def test_no_ping_while_telegram_is_unreachable(self):
        self.set_state(bot_alive=False)
        self.assertFalse(C.deadman_ping())
        self.assertEqual(self.pings, [])

    def test_no_ping_when_the_node_monitor_has_stopped(self):
        self.clock[0] += 3 * C.MONITOR_INTERVAL + 31
        self.assertFalse(C.deadman_ping())
        self.assertEqual(self.pings, [])

    def test_monitor_state_is_ignored_when_node_alerts_are_off(self):
        self.clock[0] += 10 * C.MONITOR_INTERVAL
        with mock.patch.object(C, "NODE_ALERTS", False):
            self.assertTrue(C.deadman_ping())

    def test_unreachable_ping_target_is_not_fatal(self):
        with mock.patch.object(C, "DEADMAN_URL", "http://127.0.0.1:1/x"):
            self.assertFalse(C.deadman_ping())

    def test_only_http_urls_are_used(self):
        with mock.patch.object(C, "DEADMAN_URL", "file:///etc/passwd"):
            self.assertFalse(C.deadman_ping())


class TokenStorageTest(CoordinatorTestCase):
    """Node tokens were stored verbatim in a world-readable database, so
    anyone who could read the file (a backup, another local user) could
    impersonate every node."""

    def stored_token(self, name=None):
        return self.sql("SELECT token FROM nodes WHERE name = ?", name or self.NODE)[0]["token"]

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_token_is_stored_as_a_digest_and_still_authenticates(self):
        self.assertNotEqual(self.stored_token(), self.token)
        self.assertEqual(self.stored_token(), C.token_digest(self.token))
        self.assertEqual(self.call("GET", "/commands")[0], 200)

    def test_the_stored_digest_is_not_accepted_as_a_token(self):
        headers = dict(self.auth_headers(), **{"X-Token": self.stored_token()})
        self.assertEqual(self.call("GET", "/commands", headers=headers)[0], 401)

    def test_wrong_token_is_rejected(self):
        headers = dict(self.auth_headers(), **{"X-Token": self.token[:-1] + "x"})
        self.assertEqual(self.call("GET", "/commands", headers=headers)[0], 401)

    def test_database_file_is_private_and_a_new_directory_too(self):
        self.assertEqual(self.mode(self.db_path), 0o600)
        self.assertEqual(self.mode(self.db_path.parent), 0o700)

    def test_an_existing_world_readable_database_is_tightened(self):
        os.chmod(self.db_path, 0o644)
        C.db_init()
        self.assertEqual(self.mode(self.db_path), 0o600)

    def test_an_existing_directory_is_left_alone(self):
        os.chmod(self.db_path.parent, 0o755)
        C.db_init()
        self.assertEqual(self.mode(self.db_path.parent), 0o755)

    def test_plaintext_tokens_are_migrated_with_a_rollback_copy(self):
        legacy = "legacy-plaintext-token-0123456789abcdefghijk"
        self.sql("INSERT INTO nodes (name, token, registered_at) VALUES ('old', ?, 0)", legacy)
        C.db_init()
        self.assertEqual(self.stored_token("old"), C.token_digest(legacy))

        headers = {"X-Node": "old", "X-Token": legacy}
        self.assertEqual(self.call("GET", "/commands", headers=headers)[0], 200)

        backup = self.db_path.with_name(self.db_path.name + ".pre-token-hash")
        self.assertEqual(self.mode(backup), 0o600)
        copy = sqlite3.connect(backup)
        try:
            (token,) = copy.execute("SELECT token FROM nodes WHERE name = 'old'").fetchone()
        finally:
            copy.close()
        self.assertEqual(token, legacy, "the rollback copy must hold the old format")

    def test_migration_is_idempotent_and_keeps_the_first_backup(self):
        self.sql("INSERT INTO nodes (name, token, registered_at) VALUES ('old', 'plain', 0)")
        C.db_init()
        backup = self.db_path.with_name(self.db_path.name + ".pre-token-hash")
        first = backup.read_bytes()
        digest = self.stored_token("old")
        C.db_init()
        self.assertEqual(self.stored_token("old"), digest, "digest was hashed twice")
        self.assertEqual(backup.read_bytes(), first)

    def test_no_backup_when_there_is_nothing_to_migrate(self):
        C.db_init()
        self.assertFalse(self.db_path.with_name(self.db_path.name + ".pre-token-hash").exists())

    def test_readding_a_node_replaces_its_token_and_says_so(self):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            C.cli_add_node(self.NODE)
        new = next(l.split("=", 1)[1] for l in out.getvalue().splitlines() if l.startswith("NODE_TOKEN="))
        self.assertIn("previous token no longer works", err.getvalue())
        self.assertEqual(self.call("GET", "/commands")[0], 401)  # the old one
        headers = dict(self.auth_headers(), **{"X-Token": new})
        self.assertEqual(self.call("GET", "/commands", headers=headers)[0], 200)


class RestartCardTest(CoordinatorTestCase):
    """What the operator reads after /restart. A cooldown refusal used to be
    drawn as "WARP did not come up" (the agent sent an empty report) and the
    log said "ok=False country=?"."""

    def post_result(self, meta, ok=True, output=""):
        self.queue_restart()
        (cmd,) = self.deliver()
        self.sent.clear()
        return self.call(
            "POST", "/result", {"id": cmd["id"], "ok": ok, "output": output, "meta": meta}
        ), cmd["id"]

    def test_cooldown_refusal_is_not_reported_as_a_failure(self):
        (status, _), cid = self.post_result(
            {"skipped": "cooldown", "cooldown_remaining": 42}, ok=False,
            output="restart skipped: cooldown active (42s remaining)",
        )
        self.assertEqual(status, 200)
        (message,) = self.sent
        self.assertIn(f"restart #{cid} skipped", message)
        self.assertIn("Cooldown: 42s left", message)
        self.assertNotIn("WARP did not come up", message)
        self.assertNotIn("🔴", message)

    def test_cooldown_refusal_changes_nothing_else(self):
        self.sql("UPDATE nodes SET google_country = 'NL', last_restart = 5 WHERE name = ?", self.NODE)
        with mock.patch.object(C, "RETRY_UNKNOWN_LIMIT", 5, create=True):
            self.post_result({"skipped": "cooldown", "cooldown_remaining": 10}, ok=False)
        (row,) = self.sql("SELECT google_country, last_restart FROM nodes WHERE name = ?", self.NODE)
        self.assertEqual((row["google_country"], row["last_restart"]), ("NL", 5))
        self.assertEqual(self.pending_restarts(), [], "a refusal must not schedule a retry")

    def test_failed_restart_shows_the_cause_not_a_wrong_diagnosis(self):
        self.post_result({}, ok=False,
                         output="compose restart failed:\nCannot connect to the Docker daemon")
        (message,) = self.sent
        self.assertIn("failed", message)
        self.assertIn("Reason: Cannot connect to the Docker daemon", message)
        self.assertNotIn("WARP did not come up", message)

    def test_warp_that_did_not_come_up_is_still_reported_as_such(self):
        self.post_result({"warp_up": False}, ok=True)
        self.assertIn("WARP did not come up", self.sent[0])
        self.assertIn("🔴", self.sent[0])

    def test_successful_restart_card(self):
        self.post_result({"warp_up": True, "country": "nl", "cloudflare_loc": "NL",
                          "exit_ip": "2a09:bac5:4e32:c8::14:2bd"})
        (message,) = self.sent
        self.assertTrue(message.startswith("🟢 <b>n1</b> · restart #"))
        self.assertIn("Google: 🇳🇱 Netherlands", message)
        self.assertIn("Cloudflare: 🇳🇱 Netherlands", message)
        self.assertIn("Exit IP: <code>2a09:bac5:4e32:c8::14:2bd</code>", message)

    def test_still_ru_card_is_a_warning(self):
        self.post_result({"warp_up": True, "country": "RU", "still_ru": True})
        self.assertTrue(self.sent[0].startswith("🟡"))
        self.assertIn("still RU", self.sent[0])
        self.assertIn("Google: 🇷🇺 Russia", self.sent[0])

    def test_unknown_country_card(self):
        self.post_result({"warp_up": True, "country": ""})
        self.assertIn("country unknown", self.sent[0])
        self.assertIn("Google: ❔ unknown", self.sent[0])

    def test_result_log_line_says_what_happened(self):
        with self.assertLogs(C.LOG, level="INFO") as logs:
            logging.disable(logging.NOTSET)
            try:
                self.post_result({"skipped": "cooldown", "cooldown_remaining": 42}, ok=False)
            finally:
                logging.disable(logging.CRITICAL)
        text = "\n".join(logs.output)
        self.assertIn("skipped (cooldown, 42s left)", text)
        self.assertNotIn("country=?", text)

    def test_meta_whitelist_accepts_only_known_skip_reasons(self):
        self.assertEqual(C.clean_meta({"skipped": "cooldown", "cooldown_remaining": 7})["skipped"], "cooldown")
        for bad in ("<b>x", 1, None, "other"):
            self.assertEqual(C.clean_meta({"skipped": bad})["skipped"], "", bad)
        for bad in (-1, 10**9, "7", 1.5, None, True and "x"):
            self.assertEqual(C.clean_meta({"cooldown_remaining": bad})["cooldown_remaining"], 0, bad)


class MessageHelpersTest(unittest.TestCase):
    def test_flags(self):
        self.assertEqual(C.flag("nl"), "🇳🇱")
        self.assertEqual(C.flag("RU"), "🇷🇺")
        for bad in ("", None, "N", "NLD", "1x", "<b", "é\u00e9", 5):
            self.assertEqual(C.flag(bad), "❔", bad)

    def test_place_never_echoes_unvalidated_text(self):
        self.assertEqual(C.place("de"), "🇩🇪 DE")
        self.assertEqual(C.place('<a href="x">'), "❔ unknown")

    def test_card_escapes_name_and_title(self):
        self.assertEqual(C.card("🟢", "<b>x", "a&b"), "🟢 <b>&lt;b&gt;x</b> · a&amp;b")

    def test_tree_branches(self):
        self.assertEqual(C.tree(("A", "1"), None, ("B", "2")), ["├ A: 1", "└ B: 2"])
        self.assertEqual(C.tree(("A", "1")), ["└ A: 1"])
        self.assertEqual(C.tree(), [])


class MultiWarpCoordinatorCase(CoordinatorTestCase):
    """A node with several WARPs: the agent lists them in its heartbeat."""

    def beat_warps(self, *specs, node=None, **top):
        """specs: (name, google_country, alive) tuples."""
        warps = [
            {"name": n, "google_country": c, "alive": a, "last_restart": 0}
            for n, c, a in specs
        ]
        body = {"warp_alive": all(a for _, _, a in specs), "google_country": specs[0][1], "warps": warps}
        body.update(top)
        headers = self.auth_headers() if node is None else node
        return self.call("POST", "/heartbeat", body, headers=headers)

    def commands(self, **where):
        rows = self.sql("SELECT * FROM commands WHERE result_at IS NULL ORDER BY id")
        return [(r["target"], r["id"]) for r in rows]

    def warp_rows(self, node=None):
        return self.sql("SELECT * FROM warps WHERE node = ? ORDER BY rowid", node or self.NODE)

    def add_node(self, name):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            C.cli_add_node(name)
        token = next(l.split("=", 1)[1] for l in out.getvalue().splitlines() if l.startswith("NODE_TOKEN="))
        return {"X-Node": name, "X-Token": token, "Content-Type": "application/json"}


class WarpHeartbeatTest(MultiWarpCoordinatorCase):
    def test_the_warps_of_a_node_are_stored_in_the_agents_order(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", True))
        self.assertEqual([r["name"] for r in self.warp_rows()], ["warp", "warp2"])
        self.assertEqual([r["google_country"] for r in self.warp_rows()], ["NL", "RU"])

    def test_a_warp_that_disappears_is_forgotten_with_its_alert_state(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", True))
        C.meta_set(f"warp_alert:{self.NODE}:warp2", "down")
        C.meta_set(f"retry_unknown:{self.NODE}:warp2", "1")
        self.beat_warps(("warp", "NL", True))
        self.assertEqual([r["name"] for r in self.warp_rows()], ["warp"])
        self.assertEqual(C.meta_get(f"warp_alert:{self.NODE}:warp2", "gone"), "gone")
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}:warp2", "gone"), "gone")

    def test_garbage_in_the_list_is_dropped(self):
        bad = ["x", 5, None, {"name": "bad name"}, {"name": "<b>"}, {"name": ""},
               {"name": "dup"}, {"name": "dup"}, {"name": "ok", "google_country": "<a>", "alive": "yes"}]
        self.call("POST", "/heartbeat", {"warp_alive": True, "warps": bad})
        rows = self.warp_rows()
        self.assertEqual([r["name"] for r in rows], ["dup", "ok"])
        self.assertIsNone(rows[1]["google_country"])
        self.assertEqual(rows[1]["warp_alive"], 0, "only JSON true counts as alive")

    def test_the_list_is_capped(self):
        many = [{"name": f"w{i}", "alive": True} for i in range(C.MAX_WARPS_PER_NODE + 40)]
        self.call("POST", "/heartbeat", {"warp_alive": True, "warps": many})
        self.assertEqual(len(self.warp_rows()), C.MAX_WARPS_PER_NODE)

    def test_a_missing_or_invalid_list_does_not_wipe_what_is_known(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "DE", True))
        for body in ({"warp_alive": True}, {"warp_alive": True, "warps": []},
                     {"warp_alive": True, "warps": "oops"}, {"warp_alive": True, "warps": [5]}):
            self.call("POST", "/heartbeat", body)
        self.assertEqual(len(self.warp_rows()), 2)

    def test_an_older_agent_without_a_list_has_one_implicit_warp(self):
        self.call("POST", "/heartbeat", {"warp_alive": True, "google_country": "DE"})
        row = self.sql("SELECT * FROM nodes WHERE name = ?", self.NODE)[0]
        (w,) = C.node_warps(row)
        self.assertEqual((w["name"], w["google_country"], w["warp_alive"]), ("warp", "DE", 1))
        self.assertTrue(w["implicit"])


class WarpRestartCommandTest(MultiWarpCoordinatorCase):
    def setUp(self):
        super().setUp()
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", True))

    def test_restart_without_a_warp_covers_every_warp(self):
        C.cmd_restart(self.NODE)
        self.assertEqual([t for t, _ in self.commands()], ["warp", "warp2"])
        message = self.sent[-1]
        self.assertIn("restart queued", message)
        self.assertIn("warp: #", message)
        self.assertIn("warp2: #", message)

    def test_restart_names_one_warp_in_any_case(self):
        C.cmd_restart(self.NODE, "WARP2")
        self.assertEqual([t for t, _ in self.commands()], ["warp2"])
        self.assertIn("<b>n1 / warp2</b> · restart #", self.sent[-1])
        self.assertIn("queued", self.sent[-1])

    def test_unknown_warp_queues_nothing_and_lists_the_known_ones(self):
        C.cmd_restart(self.NODE, "nope")
        self.assertEqual(self.commands(), [])
        self.assertIn("Unknown WARP", self.sent[-1])
        self.assertIn("warp, warp2", self.sent[-1])

    def test_pending_is_tracked_per_warp(self):
        C.cmd_restart(self.NODE, "warp2")
        C.cmd_restart(self.NODE, "warp2")
        self.assertIn("already pending", self.sent[-1])
        C.cmd_restart(self.NODE, "warp")           # the other one is free
        self.assertEqual([t for t, _ in self.commands()], ["warp2", "warp"])
        C.cmd_restart(self.NODE)                   # now both are pending
        self.assertIn("nothing queued", self.sent[-1])
        self.assertEqual(len(self.commands()), 2)

    def test_the_agent_is_told_which_warp_to_restart(self):
        C.cmd_restart(self.NODE, "warp2")
        self.assertEqual(self.deliver(), [{"id": 1, "cmd": "restart", "warp": "warp2"}])

    def test_telegram_syntax(self):
        C.handle_command(f"/restart {self.NODE} warp2")
        self.assertEqual([t for t, _ in self.commands()], ["warp2"])
        C.handle_command("/restart")
        self.assertIn("[warp]", self.sent[-1])
        C.handle_command("/help")
        self.assertIn("restart one WARP", self.sent[-1])

    def test_restart_all_queues_one_command_per_warp_and_skips_silent_nodes(self):
        other = self.add_node("n2")
        self.beat_warps(("warp", "NL", True), node=other)       # one WARP
        self.add_node("n3")                                      # never connected
        C.cmd_restart_all()
        self.assertEqual(sorted(t for t, _ in self.commands()), ["warp", "warp", "warp2"])
        self.assertIn("2 nodes, 3 WARPs", self.sent[-1])
        self.assertIn("<code>n3</code>: not reporting", self.sent[-1])

    def test_a_node_with_an_older_agent_gets_a_bare_command(self):
        other = self.add_node("old")
        self.call("POST", "/heartbeat", {"warp_alive": True, "google_country": "DE"}, headers=other)
        C.cmd_restart("old")
        self.assertEqual(self.sql("SELECT target FROM commands")[0]["target"], "")
        self.assertIn("⏳ <b>old</b> · restart #", self.sent[-1])  # no "/ warp" noise
        C.cmd_restart("old")
        self.assertIn("already pending", self.sent[-1])

    def test_a_single_named_warp_keeps_its_name_in_messages(self):
        other = self.add_node("solo")
        self.beat_warps(("ams", "NL", True), node=other)
        C.cmd_restart("solo")
        self.assertIn("solo / ams", self.sent[-1])
        C.cmd_restart("solo", "AMS")
        self.assertIn("already pending", self.sent[-1])


class WarpResultTest(MultiWarpCoordinatorCase):
    def setUp(self):
        super().setUp()
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", True))

    def result(self, target, meta, ok=True, output=""):
        C.cmd_restart(self.NODE, target)
        (cmd,) = [c for c in self.deliver() if c["warp"] == target]
        self.sent.clear()
        return self.call("POST", "/result", {"id": cmd["id"], "ok": ok, "output": output, "meta": meta}), cmd["id"]

    def test_the_card_names_node_and_warp(self):
        _, cid = self.result("warp2", {"warp": "warp2", "warp_up": True, "country": "NL"})
        self.assertTrue(self.sent[0].startswith(f"🟢 <b>n1 / warp2</b> · restart #{cid} · done"))

    def test_only_that_warps_row_changes(self):
        self.result("warp2", {"warp_up": True, "country": "NL"})
        rows = {r["name"]: r for r in self.warp_rows()}
        self.assertEqual((rows["warp2"]["google_country"], rows["warp"]["google_country"]), ("NL", "NL"))
        self.assertGreater(rows["warp2"]["last_restart"], 0)
        self.assertEqual(rows["warp"]["last_restart"], 0)
        # the node's own (legacy summary) columns are left to the heartbeat
        self.assertEqual(self.sql("SELECT google_country FROM nodes")[0]["google_country"], "NL")

    def test_the_exit_address_change_is_shown(self):
        self.result("warp2", {"warp_up": True, "country": "NL", "exit_ip": "2.2.2.2", "previous_ip": "1.1.1.1"})
        self.assertIn("Exit IP: <code>1.1.1.1</code> → <code>2.2.2.2</code>", self.sent[0])

    def test_an_unchanged_exit_address_is_flagged(self):
        self.result("warp2", {"warp_up": True, "country": "NL", "exit_ip": "1.1.1.1", "previous_ip": "1.1.1.1"})
        self.assertIn("<code>1.1.1.1</code> (unchanged ⚠️)", self.sent[0])

    def test_without_a_previous_address_only_the_new_one_is_shown(self):
        self.result("warp2", {"warp_up": True, "country": "NL", "exit_ip": "2.2.2.2"})
        self.assertIn("Exit IP: <code>2.2.2.2</code>", self.sent[0])
        self.assertNotIn("→", self.sent[0])

    def test_a_cooldown_refusal_names_the_warp(self):
        self.result("warp2", {"warp": "warp2", "skipped": "cooldown", "cooldown_remaining": 30}, ok=False)
        self.assertIn("n1 / warp2", self.sent[0])
        self.assertIn("skipped", self.sent[0])

    def test_unknown_country_retries_that_warp_only(self):
        with mock.patch.object(C, "RETRY_UNKNOWN_LIMIT", 3, create=True), mock.patch.object(
            C, "RETRY_UNKNOWN_DELAY", 60, create=True
        ):
            self.result("warp2", {"warp_up": True, "country": ""})
        self.assertEqual([t for t, _ in self.commands()], ["warp2"])
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}:warp2"), "1")
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}:warp", "none"), "none")
        self.assertIn("n1 / warp2", self.sent[-1])

    def test_a_known_country_clears_only_that_warps_retry_counter(self):
        C.meta_set(f"retry_unknown:{self.NODE}:warp", "2")
        C.meta_set(f"retry_unknown:{self.NODE}:warp2", "2")
        self.result("warp2", {"warp_up": True, "country": "NL"})
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}:warp2", "gone"), "gone")
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}:warp"), "2")

    def test_auto_restart_notice_names_the_warp(self):
        self.call("POST", "/notify", {"reason": "auto", "warp": "warp2", "meta": {
            "warp": "warp2", "warp_up": True, "country": "RU", "still_ru": True}})
        self.assertTrue(self.sent[-1].startswith("🟡 <b>n1 / warp2</b> · auto-restart · still RU"))

    def test_plain_notice_names_the_warp(self):
        self.call("POST", "/notify", {"text": "auto-restart of warp2 failed: boom", "warp": "warp2"})
        self.assertTrue(self.sent[-1].startswith("💬 <b>n1 / warp2</b>"))

    def test_a_hostile_warp_name_is_not_trusted(self):
        self.call("POST", "/notify", {"reason": "auto", "warp": '<a href="x">', "meta": {
            "warp": "<b>", "warp_up": True, "country": "NL"}})
        self.assertNotIn("<a href", self.sent[-1])
        self.assertNotIn("<b>&lt;", self.sent[-1])


class WarpDisplayTest(MultiWarpCoordinatorCase):
    def test_nodes_lists_each_warp_with_its_own_state(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", True))
        C.cmd_nodes()
        message = self.sent[-1]
        self.assertIn("🟡 <code>n1</code>", message)           # the worst of its WARPs
        self.assertIn("├ 🟢 warp · 🇳🇱 NL", message)
        self.assertIn("└ 🟡 warp2 · 🇷🇺 RU", message)

    def test_a_down_warp_makes_the_node_red(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "", False))
        C.cmd_nodes()
        self.assertIn("🔴 <code>n1</code>", self.sent[-1])
        self.assertIn("└ 🔴 warp2 · WARP down", self.sent[-1])

    def test_single_warp_nodes_keep_the_one_line_form(self):
        self.beat_warps(("warp", "NL", True))
        C.cmd_nodes()
        self.assertIn("🟢 <code>n1</code> · 🇳🇱 NL", self.sent[-1])
        self.assertNotIn("├", self.sent[-1])

    def test_status_lists_every_warp(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", True))
        C.cmd_status(self.NODE)
        message = self.sent[-1]
        self.assertIn("warp: 🟢 🇳🇱 Netherlands", message)
        self.assertIn("warp2: 🟡 🇷🇺 Russia", message)
        self.assertIn("Heartbeat:", message)

    def test_status_of_a_silent_node_does_not_vouch_for_its_warps(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", True))
        self.clock[0] += C.STALE_AFTER + 1
        C.cmd_status(self.NODE)
        self.assertIn("STALE", self.sent[-1])
        self.assertIn("warp2: unknown (last report: 🇷🇺 Russia, up)", self.sent[-1])

    def test_list_nodes_cli_shows_a_line_per_warp(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "RU", False))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            C.cli_list_nodes()
        lines = out.getvalue().splitlines()
        self.assertIn("WARP-DOWN", lines[0])
        self.assertIn("warp2", lines[2])
        self.assertIn("country=RU alive=no", lines[2])

    def test_bad_countries_are_configurable(self):
        self.beat_warps(("warp", "BY", True))
        C.cmd_nodes()
        self.assertIn("🟢", self.sent[-1])
        with mock.patch.object(C, "BAD_COUNTRIES", frozenset({"RU", "BY"})):
            C.cmd_nodes()
        self.assertIn("🟡 <code>n1</code> · 🇧🇾 BY", self.sent[-1])
        card = C._format_restart_message("n1", {"warp_up": True, "country": "BY"}, "restart #1")
        self.assertIn("🟢", card)
        with mock.patch.object(C, "BAD_COUNTRIES", frozenset({"BY"})):
            card = C._format_restart_message("n1", {"warp_up": True, "country": "BY"}, "restart #1")
        self.assertIn("still BY", card)

    def test_health_counts_nodes_with_any_warp_down(self):
        self.beat_warps(("warp", "NL", True), ("warp2", "", False))
        _, body = self.call("GET", "/health", headers={})
        self.assertEqual(body["nodes_warp_down"], 1)


class WarpMonitorTest(MultiWarpCoordinatorCase):
    def setUp(self):
        super().setUp()
        for name, value in {"_node_seen": {}, "_monitor_started": None}.items():
            patcher = mock.patch.object(C, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_ticks(self, *steps):
        sent = 0
        for advance, specs in steps:
            self.clock[0] += advance
            self.beat_warps(*specs)
            sent += C.node_monitor_tick()
        return sent

    UP = (("warp", "NL", True), ("warp2", "NL", True))
    DOWN2 = (("warp", "NL", True), ("warp2", "", False))

    def test_a_down_warp_is_named_once(self):
        d = C.WARP_DOWN_ALERT_AFTER
        self.run_ticks((0, self.UP), (0, self.DOWN2))
        self.assertEqual(self.run_ticks((d, self.DOWN2)), 1)
        self.assertIn("<b>n1 / warp2</b> · WARP is down", self.sent[-1])
        self.assertEqual(self.run_ticks((30, self.DOWN2), (30, self.DOWN2)), 0)

    def test_the_other_warp_is_not_blamed_and_recovery_is_per_warp(self):
        d = C.WARP_DOWN_ALERT_AFTER
        self.run_ticks((0, self.UP), (0, self.DOWN2), (d, self.DOWN2))
        self.assertEqual(self.run_ticks((10, self.UP)), 1)
        self.assertIn("<b>n1 / warp2</b> · WARP is back", self.sent[-1])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all("n1 / warp</b>" not in m for m in self.sent))

    def test_two_warps_down_make_two_alerts(self):
        d = C.WARP_DOWN_ALERT_AFTER
        both = (("warp", "", False), ("warp2", "", False))
        self.run_ticks((0, self.UP), (0, both))
        self.assertEqual(self.run_ticks((d, both)), 2)
        self.assertEqual({"n1 / warp", "n1 / warp2"}, {
            m.split("<b>")[1].split("</b>")[0] for m in self.sent})

    def test_a_silent_node_is_one_alert_not_one_per_warp(self):
        self.run_ticks((0, self.UP))
        self.clock[0] += C.STALE_AFTER + 1
        self.assertEqual(C.node_monitor_tick(), 1)
        self.assertIn("stopped reporting", self.sent[-1])
        self.assertEqual(C.node_monitor_tick(), 0)

    def test_a_warp_that_was_down_before_the_upgrade_is_not_announced_twice(self):
        self.run_ticks((0, self.UP))
        C.meta_set(f"node_alert:{self.NODE}", "warp_down")  # what the previous version stored
        self.run_ticks((0, (("warp", "", False),)), (C.WARP_DOWN_ALERT_AFTER, (("warp", "", False),)))
        self.assertEqual(self.sent, [])
        self.assertEqual(C.meta_get(f"node_alert:{self.NODE}"), "ok")

    def test_remove_node_forgets_per_warp_state_and_only_its_own(self):
        other = self.add_node("n1x")
        self.beat_warps(*self.UP)
        self.beat_warps(*self.UP, node=other)
        for node in (self.NODE, "n1x"):
            C.meta_set(f"warp_alert:{node}:warp2", "down")
            C.meta_set(f"retry_unknown:{node}:warp2", "1")
        with contextlib.redirect_stdout(io.StringIO()):
            C.cli_remove_node(self.NODE)
        self.assertEqual(self.warp_rows(), [])
        self.assertEqual(C.meta_get(f"warp_alert:{self.NODE}:warp2", "gone"), "gone")
        self.assertEqual(C.meta_get(f"retry_unknown:{self.NODE}:warp2", "gone"), "gone")
        self.assertEqual(C.meta_get("warp_alert:n1x:warp2"), "down", "another node's state was deleted")
        self.assertEqual(len(self.warp_rows("n1x")), 2)

    def test_underscore_in_a_node_name_is_not_a_wildcard(self):
        a = self.add_node("a_b")
        x = self.add_node("axb")
        C.meta_set("warp_alert:axb:warp", "down")
        with contextlib.redirect_stdout(io.StringIO()):
            C.cli_remove_node("a_b")
        self.assertEqual(C.meta_get("warp_alert:axb:warp"), "down")


class CountryDisplayTest(unittest.TestCase):
    def test_names_and_flags_for_the_usual_countries(self):
        for code, name in {"NL": "Netherlands", "DE": "Germany", "US": "United States",
                           "GB": "United Kingdom", "FR": "France", "JP": "Japan", "RU": "Russia"}.items():
            self.assertEqual(C.place(code, full=True), f"{C.flag(code)} {name}")
        self.assertEqual(C.place("nl"), "🇳🇱 NL")

    def test_a_valid_code_without_a_name_still_shows_flag_and_code(self):
        self.assertEqual(C.place("XQ", full=True), "🇽🇶 XQ")
        self.assertEqual(C.place("XQ"), "🇽🇶 XQ")

    def test_new_names_can_be_added_by_configuration(self):
        extra = C._extra_country_names("XK=Kosovo; AQ = Antarctica ,bad,=x,ZZZ=no,1A=no,NL=Holland")
        self.assertEqual(extra, {"XK": "Kosovo", "AQ": "Antarctica", "NL": "Holland"})
        with mock.patch.object(C, "COUNTRY_NAMES", {**C.COUNTRY_NAMES, **extra}):
            self.assertEqual(C.place("xk", full=True), "🇽🇰 Kosovo")
            self.assertEqual(C.place("NL", full=True), "🇳🇱 Holland")

    def test_special_codes_and_aliases(self):
        self.assertEqual(C.place("T1", full=True), "🧅 Tor")
        self.assertEqual(C.place("UK", full=True), "🇬🇧 United Kingdom")
        self.assertEqual(C.clean_country("T1"), "T1")

    def test_garbage_is_never_echoed(self):
        for bad in ("", None, 5, "N", "NLD", "1x", '<a href="x">', "é\u00e9", "XX1"):
            self.assertEqual(C.place(bad, full=True), "❔ unknown", bad)


def fresh_coordinator(**env):
    saved = dict(os.environ)
    try:
        for key in ("BAD_COUNTRIES", "COUNTRY_NAMES"):
            os.environ.pop(key, None)
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location("warp_coordinator_fresh", ROOT / "fleet" / "coordinator" / "coordinator.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


class CoordinatorEnvConfigTest(unittest.TestCase):
    def test_bad_countries(self):
        self.assertEqual(fresh_coordinator().BAD_COUNTRIES, frozenset({"RU"}))
        self.assertEqual(fresh_coordinator(BAD_COUNTRIES="ru,by").BAD_COUNTRIES, frozenset({"RU", "BY"}))
        self.assertEqual(fresh_coordinator(BAD_COUNTRIES="").BAD_COUNTRIES, frozenset())

    def test_extra_country_names_come_from_the_environment(self):
        mod = fresh_coordinator(COUNTRY_NAMES="XK=Kosovo,NL=Holland")
        self.assertEqual(mod.place("xk", full=True), "🇽🇰 Kosovo")
        self.assertEqual(mod.place("NL", full=True), "🇳🇱 Holland")
        self.assertEqual(mod.place("DE", full=True), "🇩🇪 Germany")


if __name__ == "__main__":
    unittest.main()
