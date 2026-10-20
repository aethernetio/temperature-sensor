"""Runner regression tests using receiver logs and a virtual clock (no hardware)."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_runner():
    stubs = {
        name: types.ModuleType(name)
        for name in (
            "run_thermometer2_sleep_bisect",
            "run_thermometer2_aether_full_vanilla",
        )
    }
    stubs["run_thermometer2_sleep_bisect"].PpkSession = object
    path = Path(__file__).with_name("run_simple_full_hot_101.py")
    spec = importlib.util.spec_from_file_location("_smoke_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    with (
        patch.dict(sys.modules, stubs),
        patch.object(sys, "path", sys.path.copy()),
        patch.dict(os.environ, {}, clear=False),
        patch.object(Path, "is_file", return_value=False),
    ):
        spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def wait_rx(self, *, numbers: list[int], sender: str | None = None) -> tuple[dict, float]:
        sender = sender or "00000000-0000-0000-0000-000000000001"
        state = {
            "run_status": "RUNNING",
            "test_sender_uid": sender,
            "expected_sender_uid": sender,
            "campaign_log_offset": 0,
            "measured_campaign_deadline_s": 3600.0,
        }
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as folder:
            receiver_log = Path(folder) / "receiver.log"
            receiver_log.write_text(
                "".join(f"MSG_NUM={n} sender={sender}\n" for n in numbers),
                encoding="utf-8",
            )
            with (
                patch.object(self.runner, "SystemMonotonicClock", return_value=clock),
                patch.object(self.runner.time, "sleep", side_effect=clock.sleep),
                patch("builtins.print"),
            ):
                self.runner.wait_campaign(
                    rx_log=receiver_log,
                    rx_proc=Mock(poll=Mock(return_value=None)),
                    writer=Mock(),
                    state=state,
                    interrupted={"flag": False},
                )
        return state, clock.t

    def test_tail_finishes_30s_after_101_even_if_2_missing(self):
        sender = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        nums = [1] + list(range(3, self.runner.LAST_MESSAGE + 1))
        state, elapsed = self.wait_rx(numbers=nums, sender=sender)
        self.runner.finalize_test_status(state)
        self.assertEqual(elapsed, self.runner.TAIL_DELIVERY_S)
        self.assertIn(2, state["missing_numbers"])
        self.assertFalse(state["pass"])
        self.assertEqual(state["DELIVERY"], "PARTIAL")

    def test_full_1_101_passes_delivery(self):
        nums = list(range(1, self.runner.LAST_MESSAGE + 1))
        state, _ = self.wait_rx(numbers=nums)
        self.runner.finalize_test_status(state)
        self.assertTrue(state["pass"])
        self.assertEqual(state["DELIVERY"], "PASS")
        self.assertEqual(state["TEST"], "PASS")

    def test_aborted_never_becomes_test_pass(self):
        state = {
            "run_status": "ABORTED",
            "runner_interrupted": True,
            "pass": True,
            "msg_rx_count": 101,
            "TEST": "NOT_RUN",
        }
        self.runner.finalize_test_status(state)
        self.assertEqual(state["TEST"], "ABORTED")

    def test_timeout_stays_timeout(self):
        state = {
            "run_status": "TIMEOUT",
            "pass": False,
            "msg_rx_count": 50,
            "TEST": "NOT_RUN",
        }
        self.runner.finalize_test_status(state)
        self.assertEqual(state["TEST"], "TIMEOUT")


if __name__ == "__main__":
    unittest.main()
