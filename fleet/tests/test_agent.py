"""Tests for fleet/agent/agent.py: the restart cooldown is shared between
coordinator-issued (manual) and automatic restarts.

Run with:  python3 -m unittest discover -s fleet/tests -v
"""

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
