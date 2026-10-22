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
        self.assertIn("warp_alive: unknown", message)
        self.assertIn("google: unknown", message)
        self.assertNotIn("warp_alive: yes", message)

    def test_status_of_a_fresh_node_is_unchanged(self):
        self.beat()
        C.cmd_status(self.NODE)
        self.assertIn("warp_alive: yes", self.sent[-1])
        self.assertIn("google: DE", self.sent[-1])

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
        self.assertIn("WARP proxy is down", self.sent[-1])
        self.beat(warp_alive=True)
        self.assertEqual(self.tick(), 1)
        self.assertIn("WARP proxy is back", self.sent[-1])

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
        self.assertIn("Queued", self.sent[-1])
        self.clock[0] += 18 * 86400
        C.cmd_restart(self.NODE)
        self.assertIn("Queued", self.sent[-1])
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
        self.assertIn("Queued restart for <b>2</b>", message)
        self.assertIn("silent (not reporting)", message)
        self.assertIn("busy (already pending)", message)

    def test_old_finished_commands_are_pruned(self):
        t = self.clock[0]
        old, recent = 40 * 86400, 5 * 86400
        for result_at in (t - old, t - recent, None):
            self.sql("INSERT INTO commands (node, command, created_at, result_at) "
                     "VALUES (?, 'restart', ?, ?)", self.NODE, t - old, result_at)
        self.assertEqual(C.prune_commands(), 1)
        self.assertEqual(len(self.sql("SELECT id FROM commands")), 2)


if __name__ == "__main__":
    unittest.main()
