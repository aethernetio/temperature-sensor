"""Runner regression tests using log files and a virtual clock, without hardware."""
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
    """Replace only hardware imports; execute the real runner and log parsers."""
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
        patch.dict(os.environ, {"SIMPLE_FULL_HOT_COUNT": "5"}),
        # Disable optional Windows-venv re-exec while importing for a dry test.
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

    def campaign(self, *, numbers, serial_text):
        sender = "00000000-0000-0000-0000-000000000001"
        state = {"run_status": "RUNNING", "test_sender_uid": sender}
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as folder:
            receiver_log = Path(folder) / "receiver.log"
            serial_log = Path(folder) / "serial.log"
            receiver_log.write_text(
                "".join(f"MSG_NUM={n} sender={sender}\n" for n in numbers),
                encoding="utf-8",
            )
            serial_log.write_text(serial_text, encoding="utf-8")
            with (
                patch.object(self.runner, "SystemMonotonicClock", return_value=clock),
                patch.object(self.runner.time, "sleep", side_effect=clock.sleep),
                patch("builtins.print"),
            ):
                self.runner.wait_campaign(
                    rx_log=receiver_log,
                    serial_log=serial_log,
                    rx_proc=Mock(poll=Mock(return_value=None)),
                    writer=Mock(),
                    state=state,
                )
        return state, clock.t

    def test_old_boot_marker_does_not_keep_usb_alive(self):
        state, elapsed = self.campaign(
            numbers=[], serial_text="BOOT reset_reason=1 next_message=1\n"
        )
        self.assertEqual(state["run_status"], "TIMEOUT")
        self.assertEqual(state["error"], "usb_startup_timeout_no_full_begin")
        self.assertLess(elapsed, 200.0)

    def test_hot_timeout_without_full_begin_marker(self):
        state, elapsed = self.campaign(
            numbers=[1, 3], serial_text="BOOTSTRAP_DONE\n"
        )
        self.assertEqual(state["run_status"], "TIMEOUT")
        self.assertEqual(state["error"], "hot_cycle_timeout")
        self.assertLess(elapsed, 200.0)

    def test_tail_finishes_even_when_middle_message_is_missing(self):
        state, elapsed = self.campaign(
            numbers=[1, 3, 4, 5, 6], serial_text="BOOTSTRAP_DONE\n"
        )
        self.runner.finalize_test_status(state)
        self.assertEqual(elapsed, 30.0)
        self.assertEqual(state["missing_numbers"], [2])
        self.assertEqual(state["HOT_RX"], "4/5")
        self.assertEqual(state["DELIVERY"], "PARTIAL")
        self.assertEqual(state["EXECUTION"], "COMPLETED")

    def test_normal_finalization_reports_completed(self):
        for hot_rx, rtc_complete, run_status in (
            ("5/5", True, "PASS"),
            ("4/5", False, "DONE"),
        ):
            with self.subTest(hot_rx=hot_rx):
                state = {
                    "run_status": "RUNNING",
                    "FULL_RX": "1/1",
                    "HOT_RX": hot_rx,
                    "hot_rtc_table_complete": rtc_complete,
                    "TEST": "NOT_RUN",
                }
                self.runner.finalize_test_status(state)
                self.assertEqual(state["run_status"], run_status)
                self.assertEqual(state["EXECUTION"], "COMPLETED")

    def test_timeout_and_abort_remain_distinct(self):
        for status in ("TIMEOUT", "ABORTED"):
            with self.subTest(status=status):
                state = {
                    "run_status": status,
                    "FULL_RX": "1/1",
                    "HOT_RX": "4/5",
                    "TEST": "NOT_RUN",
                }
                self.runner.finalize_test_status(state)
                self.assertEqual(state["run_status"], status)
                self.assertEqual(state["EXECUTION"], status)
                self.assertEqual(state["DELIVERY"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
