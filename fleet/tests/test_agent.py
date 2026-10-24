"""Tests for fleet/agent/agent.py: the restart cooldown is shared between
coordinator-issued (manual) and automatic restarts.

Run with:  python3 -m unittest discover -s fleet/tests -v
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]

os.environ.setdefault("COORDINATOR_URL", "http://127.0.0.1:1")
os.environ.setdefault("NODE_NAME", "test-node")
os.environ.setdefault("NODE_TOKEN", "test-token")

_spec = importlib.util.spec_from_file_location(
    "warp_agent", ROOT / "fleet" / "agent" / "agent.py"
)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)


class _StopLoop(Exception):
    """Raised from the patched sleep() to leave main()'s infinite loop."""


OK_REPORT = {"warp_up": True, "country": "DE", "ip": "1.2.3.4"}


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = Path(tmp.name)
        for name, value in {
            "STATE_DIR": state_dir,
            "STATE_FILE": state_dir / "state.json",
            "PENDING_DIR": state_dir / "pending",
            "RESTART_COOLDOWN": 300,
        }.items():
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        state = mock.patch.dict(
            agent.STATE, {"warps": {}, "heartbeat_now": False}
        )
        state.start()
        self.addCleanup(state.stop)

        self.posts = []

        def fake_post(path, payload):
            self.posts.append((path, payload))
            return True, {}

        for name, new in {
            "http_post": fake_post,
            "queue_result": mock.Mock(),
        }.items():
            patcher = mock.patch.object(agent, name, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def results(self):
        return [p for path, p in self.posts if path == "/result"]


class ManualRestartCooldownTest(AgentTestCase):
    def test_skipped_while_cooldown_is_active(self):
        agent.set_last_restart(int(time.time()) - 10)
        with mock.patch.object(agent, "do_restart_and_report") as restart:
            agent.handle_command({"id": 7, "cmd": "restart"})
        restart.assert_not_called()
        (result,) = self.results()
        self.assertEqual(result["id"], 7)
        self.assertFalse(result["ok"])
        self.assertIn("cooldown", result["output"])
        # Not an empty report: the coordinator would read that as "WARP down".
        self.assertEqual(result["meta"]["skipped"], "cooldown")
        self.assertGreater(result["meta"]["cooldown_remaining"], 0)
        self.assertEqual(agent.warp_state()["google_country"], "")
        self.assertFalse(agent.STATE["heartbeat_now"])

    def test_runs_after_cooldown_and_starts_a_new_one(self):
        agent.set_last_restart(int(time.time()) - 1000)
        with mock.patch.object(
            agent, "do_restart_and_report", return_value=(True, "done", OK_REPORT)
        ) as restart:
            agent.handle_command({"id": 8, "cmd": "restart"})
        restart.assert_called_once()
        (result,) = self.results()
        self.assertTrue(result["ok"])
        self.assertGreater(agent.cooldown_remaining(), 0)

    def test_failed_restart_does_not_start_cooldown(self):
        agent.set_last_restart(0)
        with mock.patch.object(
            agent, "do_restart_and_report", return_value=(False, "boom", {})
        ):
            agent.handle_command({"id": 9, "cmd": "restart"})
        self.assertEqual(agent.cooldown_remaining(), 0)

    def test_unknown_command_is_rejected(self):
        agent.handle_command({"id": 10, "cmd": "rm -rf"})
        agent.queue_result.assert_called_once()
        self.assertFalse(agent.queue_result.call_args.args[1])


class AutoRestartSharesCooldownTest(AgentTestCase):
    """Regression: main() cached last_restart once at startup and only
    refreshed it on *automatic* restarts, so a restart ordered through
    /restart did not delay the next automatic one."""

    def run_main(self, countries, commands, last_restart):
        agent.set_last_restart(last_restart)
        sleeps = iter([None] * (len(countries) - 1))

        def fake_sleep(_seconds):
            try:
                next(sleeps)
            except StopIteration:
                raise _StopLoop

        http_get = mock.Mock(
            side_effect=[(True, {"commands": cmds}) for cmds in commands]
            + [(False, None)] * 10
        )
        restart = mock.Mock(return_value=(True, "ok", OK_REPORT))
        with mock.patch.object(agent, "CHECK_INTERVAL", 0), mock.patch.object(
            agent, "warp_alive", return_value=True
        ), mock.patch.object(
            agent, "detect_google_country", side_effect=countries
        ), mock.patch.object(
            agent, "http_get", http_get
        ), mock.patch.object(
            agent, "flush_pending"
        ), mock.patch.object(
            agent, "do_restart_and_report", restart
        ), mock.patch.object(
            agent.time, "sleep", fake_sleep
        ):
            with self.assertRaises(_StopLoop):
                agent.main()
        return restart

    def test_manual_restart_delays_the_next_auto_restart(self):
        # Iteration 1: Google=DE, the operator orders /restart.
        # Iteration 2: Google=RU again, but the cooldown just started.
        restart = self.run_main(
            countries=["DE", "RU"],
            commands=[[{"id": 1, "cmd": "restart"}], []],
            last_restart=0,
        )
        self.assertEqual(restart.call_count, 1, "auto-restart ignored the cooldown")

    def test_auto_restart_still_fires_once_cooldown_expired(self):
        restart = self.run_main(
            countries=["RU"],
            commands=[[]],
            last_restart=int(time.time()) - 1000,
        )
        self.assertEqual(restart.call_count, 1)


class HeartbeatAfterRestartTest(AgentTestCase):
    """The heartbeat reported the country of the last *periodic check*, so
    after a restart it repeated the old value (RU) and overwrote the fresh
    one (NL) the coordinator had just received with the restart result."""

    def heartbeats(self):
        return [p for path, p in self.posts if path == "/heartbeat"]

    def test_manual_restart_updates_what_the_heartbeat_reports(self):
        agent.set_last_restart(0)
        report = {"warp_up": True, "country": "NL", "cloudflare_loc": "NL"}
        with mock.patch.object(
            agent, "do_restart_and_report", return_value=(True, "ok", report)
        ):
            agent.handle_command({"id": 1, "cmd": "restart"})
        self.assertEqual(agent.warp_state()["google_country"], "NL")
        self.assertTrue(agent.warp_state()["alive"])
        self.assertTrue(agent.STATE["heartbeat_now"])

    def test_restart_where_warp_did_not_come_up_is_reported_as_down(self):
        agent.warp_state().update(alive=True, google_country="RU")
        agent.remember_restart({"warp_up": False, "country": ""})
        self.assertFalse(agent.warp_state()["alive"])
        self.assertEqual(agent.warp_state()["google_country"], "")

    def test_unknown_country_is_not_reported_as_a_country(self):
        agent.remember_restart({"warp_up": True, "country": "?"})
        self.assertEqual(agent.warp_state()["google_country"], "")

    def run_main(self, countries, commands, restart_report, iterations):
        sleeps = iter([None] * (iterations - 1))

        def fake_sleep(_seconds):
            try:
                next(sleeps)
            except StopIteration:
                raise _StopLoop

        http_get = mock.Mock(
            side_effect=[(True, {"commands": c}) for c in commands] + [(False, None)] * 10
        )
        restart = mock.Mock(return_value=(True, "ok", restart_report))
        # CHECK_INTERVAL is huge: the periodic check runs once, in the first
        # iteration, as it would between two real checks.
        with mock.patch.object(agent, "CHECK_INTERVAL", 10**6), mock.patch.object(
            agent, "HEARTBEAT_INTERVAL", 10**6
        ), mock.patch.object(agent, "warp_alive", return_value=True), mock.patch.object(
            agent, "detect_google_country", side_effect=countries
        ), mock.patch.object(agent, "http_get", http_get), mock.patch.object(
            agent, "flush_pending"
        ), mock.patch.object(agent, "do_restart_and_report", restart), mock.patch.object(
            agent.time, "sleep", fake_sleep
        ):
            with self.assertRaises(_StopLoop):
                agent.main()

    def test_heartbeat_after_a_manual_restart_carries_the_new_country(self):
        agent.set_last_restart(0)
        # Iteration 1: check says DE, the operator orders /restart -> NL.
        # Iteration 2: the heartbeat must say NL at once (the interval is
        # huge, so only the post-restart trigger can send it) and must not
        # fall back to the last periodic check.
        self.run_main(
            countries=["DE"],
            commands=[[{"id": 1, "cmd": "restart"}], []],
            restart_report={"warp_up": True, "country": "NL"},
            iterations=2,
        )
        beats = self.heartbeats()
        self.assertEqual(len(beats), 2, "no heartbeat after the restart")
        self.assertEqual(beats[0]["google_country"], "DE")
        self.assertEqual(beats[1]["google_country"], "NL")
        self.assertTrue(beats[1]["warp_alive"])

    def test_heartbeat_after_an_auto_restart_carries_the_new_country(self):
        agent.set_last_restart(0)
        self.run_main(
            countries=["RU"],
            commands=[[]],
            restart_report={"warp_up": True, "country": "NL"},
            iterations=1,
        )
        (beat,) = self.heartbeats()
        self.assertEqual(beat["google_country"], "NL", "stale pre-restart country sent")


def make_warps(*names):
    """Warp objects 'a', 'b', ... with their own proxy port and service."""
    return [
        agent.Warp(n, f"127.0.0.1:{1080 + i}", f"svc-{n}", "/opt/warp", "docker-compose.yml")
        for i, n in enumerate(names)
    ]


class WarpConfigTest(unittest.TestCase):
    """WARPs are discovered from WARP<N>_* variables: any number, added by
    appending a group of lines to the env file."""

    def load(self, **env):
        return agent.load_warps(env)

    def test_without_extra_variables_there_is_one_warp_as_before(self):
        (w,) = self.load()
        self.assertEqual((w.name, w.proxy, w.service), ("warp", "127.0.0.1:1080", "warp"))

    def test_a_second_warp(self):
        first, second = self.load(WARP2_PROXY="127.0.0.1:1081", WARP2_SERVICE="warp2")
        self.assertEqual(first.name, "warp")
        self.assertEqual((second.name, second.proxy, second.service), ("warp2", "127.0.0.1:1081", "warp2"))

    def test_docker_port_mapping_form_is_accepted(self):
        (_, second) = self.load(WARP2_PROXY="127.0.0.1:1081:1080", WARP2_SERVICE="warp2")
        self.assertEqual(second.proxy, "127.0.0.1:1081")
        self.assertEqual(agent.normalize_proxy("127.0.0.1:1080"), "127.0.0.1:1080")
        self.assertEqual(agent.normalize_proxy(" [::1]:1080 "), "[::1]:1080")

    def test_there_is_no_limit_and_numbers_sort_numerically(self):
        env = {}
        for n in range(2, 13):
            env[f"WARP{n}_PROXY"] = f"127.0.0.1:{1079 + n}"
            env[f"WARP{n}_SERVICE"] = f"w{n}"
        warps = self.load(**env)
        self.assertEqual(len(warps), 12)
        self.assertEqual([w.service for w in warps][-3:], ["w10", "w11", "w12"])

    def test_names_can_be_set(self):
        (first, second) = self.load(
            WARP1_NAME="ams", WARP2_NAME="fra", WARP2_PROXY="127.0.0.1:1081", WARP2_SERVICE="warp-b"
        )
        self.assertEqual((first.name, second.name), ("ams", "fra"))

    def test_per_warp_compose_location(self):
        (_, second) = self.load(
            WARP2_PROXY="127.0.0.1:1081", WARP2_SERVICE="w", WARP2_COMPOSE_DIR="/opt/other",
            WARP2_COMPOSE_FILE="b.yml",
        )
        self.assertEqual((second.compose_dir, second.compose_file), ("/opt/other", "b.yml"))

    def test_incomplete_or_conflicting_settings_are_refused(self):
        bad = [
            {"WARP2_SERVICE": "w"},  # no proxy
            {"WARP2_PROXY": "127.0.0.1:1081"},  # nothing to restart
            {"WARP2_PROXY": "127.0.0.1:1081", "WARP2_SERVICE": "warp"},  # same service as #1
            {"WARP2_PROXY": "127.0.0.1:1080", "WARP2_SERVICE": "w"},  # same proxy as #1
            {"WARP2_PROXY": "127.0.0.1:1081", "WARP2_SERVICE": "w", "WARP2_NAME": "warp"},  # same name
            {"WARP2_PROXY": "127.0.0.1:1081", "WARP2_SERVICE": "w", "WARP2_NAME": "bad name"},
            {"WARP2_PROXY": "127.0.0.1:1081", "WARP2_SERVICE": "w", "WARP2_NAME": "<b>"},
        ]
        for env in bad:
            with self.assertRaises(agent.ConfigError, msg=env):
                self.load(**env)


class MultiWarpTestCase(AgentTestCase):
    def setUp(self):
        super().setUp()
        warps = mock.patch.object(agent, "WARPS", make_warps("a", "b"))
        warps.start()
        self.addCleanup(warps.stop)
        self.a, self.b = agent.WARPS

    def restart_stub(self, report=None):
        calls = []

        def fake(w=None):
            calls.append(w.name)
            return True, f"restarted {w.name}", dict(report or OK_REPORT, warp=w.name)

        return mock.patch.object(agent, "do_restart_and_report", side_effect=fake), calls


class PerWarpCooldownTest(MultiWarpTestCase):
    def test_each_warp_has_its_own_cooldown(self):
        agent.set_last_restart(int(time.time()) - 10, "a")
        self.assertGreater(agent.cooldown_remaining(name="a"), 0)
        self.assertEqual(agent.cooldown_remaining(name="b"), 0)

    def test_state_file_of_a_single_warp_install_still_counts(self):
        agent.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        agent.STATE_FILE.write_text('{"last_restart": %d}' % (int(time.time()) - 5))
        self.assertGreater(agent.cooldown_remaining(name="a"), 0, "first WARP keeps the legacy value")
        self.assertEqual(agent.cooldown_remaining(name="b"), 0)

    def test_the_legacy_key_follows_the_first_warp_only(self):
        agent.set_last_restart(1234, "b")
        self.assertNotEqual(agent.get_last_restart("a"), 1234)
        agent.set_last_restart(5678, "a")
        self.assertEqual(agent.get_last_restart("a"), 5678)
        self.assertEqual(agent.get_last_restart("b"), 1234)


class TargetedRestartTest(MultiWarpTestCase):
    def test_a_named_warp_is_restarted_alone(self):
        patcher, calls = self.restart_stub()
        with patcher:
            agent.handle_command({"id": 1, "cmd": "restart", "warp": "b"})
        self.assertEqual(calls, ["b"])
        (result,) = self.results()
        self.assertTrue(result["ok"])
        self.assertEqual(result["meta"]["warp"], "b")
        self.assertGreater(agent.cooldown_remaining(name="b"), 0)
        self.assertEqual(agent.cooldown_remaining(name="a"), 0)

    def test_cooldown_refusal_names_the_warp(self):
        agent.set_last_restart(int(time.time()) - 5, "a")
        patcher, calls = self.restart_stub()
        with patcher:
            agent.handle_command({"id": 2, "cmd": "restart", "warp": "a"})
        self.assertEqual(calls, [])
        (result,) = self.results()
        self.assertEqual(result["meta"]["skipped"], "cooldown")
        self.assertEqual(result["meta"]["warp"], "a")
        self.assertIn("[a]", result["output"])

    def test_unknown_warp_is_an_error_that_lists_the_known_ones(self):
        patcher, calls = self.restart_stub()
        with patcher:
            agent.handle_command({"id": 3, "cmd": "restart", "warp": "zzz"})
        self.assertEqual(calls, [])
        (result,) = self.results()
        self.assertFalse(result["ok"])
        self.assertIn("unknown warp", result["output"])
        self.assertIn("a, b", result["output"])

    def test_a_bare_restart_covers_every_warp_of_the_node(self):
        patcher, calls = self.restart_stub()
        with patcher:
            agent.handle_command({"id": 4, "cmd": "restart"})
        self.assertEqual(calls, ["a", "b"])
        (result,) = self.results()
        self.assertTrue(result["ok"])
        self.assertIn("restarted a", result["output"])
        self.assertIn("restarted b", result["output"])

    def test_restarting_one_warp_updates_only_its_heartbeat_state(self):
        patcher, _ = self.restart_stub({"warp_up": True, "country": "NL"})
        with patcher:
            agent.handle_command({"id": 5, "cmd": "restart", "warp": "b"})
        self.assertEqual(agent.warp_state("b")["google_country"], "NL")
        self.assertEqual(agent.warp_state("a")["google_country"], "")
        self.assertTrue(agent.STATE["heartbeat_now"])


class MultiWarpHeartbeatTest(MultiWarpTestCase):
    def test_heartbeat_lists_every_warp(self):
        agent.warp_state("a").update(alive=True, google_country="NL")
        agent.warp_state("b").update(alive=True, google_country="DE")
        agent.set_last_restart(100, "a")
        agent.set_last_restart(200, "b")
        beat = agent.heartbeat_payload()
        self.assertEqual([w["name"] for w in beat["warps"]], ["a", "b"])
        self.assertEqual(beat["warps"][1]["google_country"], "DE")
        self.assertEqual((beat["warps"][0]["last_restart"], beat["last_restart"]), (100, 200))

    def test_legacy_fields_point_at_the_worst_warp(self):
        agent.warp_state("a").update(alive=True, google_country="NL")
        agent.warp_state("b").update(alive=True, google_country="RU")
        beat = agent.heartbeat_payload()
        self.assertEqual(beat["google_country"], "RU")
        self.assertTrue(beat["warp_alive"])

    def test_node_is_not_alive_if_any_warp_is_down(self):
        agent.warp_state("a").update(alive=True, google_country="NL")
        agent.warp_state("b").update(alive=False, google_country="")
        self.assertFalse(agent.heartbeat_payload()["warp_alive"])


class MultiWarpCheckTest(MultiWarpTestCase):
    def run_check(self, countries, alive=None, cooldown_a=0):
        """countries: what Google says per WARP; returns the WARPs restarted."""
        restarted = []

        def fake_restart(w=None):
            restarted.append(w.name)
            return True, "ok", dict(OK_REPORT, warp=w.name, country="NL")

        with mock.patch.object(
            agent, "warp_alive", side_effect=lambda w=None: (alive or {}).get(w.name, True)
        ), mock.patch.object(
            agent, "detect_google_country", side_effect=lambda w=None: countries[w.name]
        ), mock.patch.object(agent, "do_restart_and_report", side_effect=fake_restart):
            for w in agent.WARPS:
                agent.check_warp(w, time.time())
        return restarted

    def test_only_the_warp_in_a_bad_country_is_restarted(self):
        self.assertEqual(self.run_check({"a": "NL", "b": "RU"}), ["b"])
        notify = [p for path, p in self.posts if path == "/notify"]
        self.assertEqual(len(notify), 1)
        self.assertEqual(notify[0]["warp"], "b")
        self.assertIn("b", notify[0]["text"])
        self.assertEqual(notify[0]["meta"]["warp"], "b")

    def test_both_are_restarted_when_both_are_bad(self):
        self.assertEqual(self.run_check({"a": "RU", "b": "RU"}), ["a", "b"])

    def test_one_warps_cooldown_does_not_hold_back_the_other(self):
        agent.set_last_restart(int(time.time()) - 5, "a")
        self.assertEqual(self.run_check({"a": "RU", "b": "RU"}), ["b"])

    def test_a_down_warp_is_recorded_and_the_other_still_checked(self):
        self.assertEqual(self.run_check({"a": "", "b": "RU"}, alive={"a": False}), ["b"])
        self.assertFalse(agent.warp_state("a")["alive"])
        self.assertEqual(agent.warp_state("b")["google_country"], "NL")  # after its restart

    def test_bad_countries_are_configurable(self):
        with mock.patch.object(agent, "BAD_COUNTRIES", frozenset({"RU", "BY"})):
            self.assertEqual(self.run_check({"a": "BY", "b": "NL"}), ["a"])
        with mock.patch.object(agent, "BAD_COUNTRIES", frozenset({"RU"})):
            self.assertEqual(self.run_check({"a": "BY", "b": "NL"}), [])

    def test_probes_ask_through_the_warps_own_proxy(self):
        seen = []

        def fake_curl(opts, url):
            seen.append(opts[opts.index("--socks5-hostname") + 1])
            return "ip=1.1.1.1\nloc=NL\n"

        with mock.patch.object(agent, "_curl", side_effect=fake_curl):
            agent.warp_alive(self.b)
            agent.warp_exit_ip(self.a)
        self.assertEqual(seen, ["127.0.0.1:1081", "127.0.0.1:1080"])

    def test_restart_targets_the_warps_own_compose_service(self):
        with mock.patch.object(agent.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            agent.compose_restart(self.b)
        self.assertEqual(run.call_args.args[0][-2:], ["restart", "svc-b"])


class PreviousIpTest(MultiWarpTestCase):
    def restart_with_ips(self, before, after):
        with mock.patch.object(agent, "warp_exit_ip", side_effect=[before, after]), mock.patch.object(
            agent, "compose_restart", return_value=(True, "")
        ), mock.patch.object(agent, "wait_warp_up", return_value=True), mock.patch.object(
            agent, "detect_google_country", return_value="NL"
        ), mock.patch.object(agent, "warp_exit_country", return_value="NL"):
            return agent.do_restart_and_report(self.a)

    def test_report_carries_the_address_before_and_after(self):
        ok, text, report = self.restart_with_ips("1.1.1.1", "2.2.2.2")
        self.assertTrue(ok)
        self.assertEqual((report["previous_ip"], report["exit_ip"], report["warp"]), ("1.1.1.1", "2.2.2.2", "a"))
        self.assertIn("1.1.1.1 -> 2.2.2.2", text)

    def test_an_unchanged_address_is_called_out(self):
        _, text, report = self.restart_with_ips("1.1.1.1", "1.1.1.1")
        self.assertEqual(report["previous_ip"], report["exit_ip"])
        self.assertIn("(unchanged)", text)


class StatusCliTest(MultiWarpTestCase):
    def test_status_prints_every_warp(self):
        out = io.StringIO()
        with mock.patch.object(
            agent, "warp_alive", side_effect=lambda w=None: w.name == "a"
        ), mock.patch.object(agent, "detect_google_country", return_value="NL"), mock.patch.object(
            agent, "warp_exit_ip", return_value="9.9.9.9"
        ), contextlib.redirect_stdout(out):
            self.assertEqual(agent.cli_status(), 0)
        text = out.getvalue()
        self.assertIn("a ", text)
        self.assertIn("proxy=127.0.0.1:1081", text)
        self.assertIn("DOWN", text)
        self.assertIn("google=NL", text)
        self.assertIn("ip=9.9.9.9", text)


def fresh_agent(**env):
    """A new copy of the agent module, read with the given environment."""
    base = {"COORDINATOR_URL": "http://x", "NODE_NAME": "n", "NODE_TOKEN": "t"}
    saved = dict(os.environ)
    try:
        os.environ.pop("BAD_COUNTRIES", None)
        os.environ.update(base)
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location("warp_agent_fresh", ROOT / "fleet" / "agent" / "agent.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


class BadCountriesConfigTest(unittest.TestCase):
    def test_default_is_russia(self):
        self.assertEqual(fresh_agent().BAD_COUNTRIES, frozenset({"RU"}))

    def test_space_and_comma_separated_any_case(self):
        self.assertEqual(
            fresh_agent(BAD_COUNTRIES="ru, by  ir,cn").BAD_COUNTRIES, frozenset({"RU", "BY", "IR", "CN"})
        )

    def test_empty_means_no_country_is_refused(self):
        self.assertEqual(fresh_agent(BAD_COUNTRIES="").BAD_COUNTRIES, frozenset())


if __name__ == "__main__":
    unittest.main()
