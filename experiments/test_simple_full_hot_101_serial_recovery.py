"""Deterministic serial recovery tests without hardware or real waits."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import simple_full_hot_101_serial as serial_capture


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class RecoveryBackend:
    def __init__(
        self,
        clock: FakeClock,
        *,
        recovery_available: bool,
        recovery_read_fails: bool,
    ) -> None:
        self.clock = clock
        self.recovery_available = recovery_available
        self.recovery_read_fails = recovery_read_fails
        self.handles: list[SimpleNamespace] = []
        self.events: list[tuple[str, float, object]] = []
        self.concurrent_open_attempts = 0
        self.disconnect_at: float | None = None
        self.capture: serial_capture.SerialCapture | None = None

    def open(self, port: str):
        self.events.append(("open_attempt", self.clock.now, port))
        if any(handle.is_open for handle in self.handles):
            self.concurrent_open_attempts += 1
            raise RuntimeError("previous handle is still open")
        if self.handles and not self.recovery_available:
            raise OSError("recovery port unavailable")
        handle = SimpleNamespace(
            is_open=True, number=len(self.handles) + 1, reads=0
        )
        self.handles.append(handle)
        self.events.append(("open", self.clock.now, handle.number))
        return handle

    def close(self, handle) -> None:
        self.events.append(("close", self.clock.now, handle.number))
        handle.is_open = False

    def read(self, handle, size: int) -> bytes:
        handle.reads += 1
        if handle.number == 1:
            if handle.reads == 1:
                self.clock.now += 60.0
                return b"BOOT reset_reason=0 next_message=1\n"
            self.disconnect_at = self.clock.now
            raise OSError("ClearCommError (simulated)")
        if self.recovery_read_fails:
            raise OSError("recovery read failed immediately")
        assert self.capture is not None
        self.capture._stop.set()
        return b"RTC_CAPTURE_RECOVERED\n"


class SerialRecoveryTests(unittest.TestCase):
    def run_capture(
        self, *, recovery_available: bool = True, recovery_read_fails: bool = False
    ):
        clock = FakeClock()
        backend = RecoveryBackend(
            clock,
            recovery_available=recovery_available,
            recovery_read_fails=recovery_read_fails,
        )
        with tempfile.TemporaryDirectory(prefix="serial_recovery_test_") as directory:
            log_path = Path(directory) / "serial.log"
            capture = serial_capture.SerialCapture(
                log_path,
                backend=backend,
                port_hint="COM9",
                discover=lambda: "COM10",
            )
            backend.capture = capture
            with patch.object(serial_capture, "time", clock):
                # Run synchronously: no capture thread, COM access, or real sleep.
                capture._run()
            log = log_path.read_text(encoding="utf-8")
        return clock, backend, capture, log

    def test_disconnect_after_60_seconds_reopens(self):
        _, backend, capture, log = self.run_capture()
        self.assertEqual(len(backend.handles), 2, log)
        self.assertIn("SERIAL_READ_ERROR=ClearCommError (simulated)", log)
        self.assertIn("SERIAL_REDISCOVER", log)
        self.assertIn("RTC_CAPTURE_RECOVERED", log)
        self.assertNotIn("SERIAL_CAPTURE_ERROR=open_timeout", log)
        self.assertFalse(capture.handle_open)

    def test_old_handle_is_closed_before_reopen(self):
        _, backend, _, log = self.run_capture()
        lifecycle = [
            (kind, handle_number)
            for kind, _, handle_number in backend.events
            if kind in {"open", "close"}
        ]
        self.assertEqual(
            lifecycle, [("open", 1), ("close", 1), ("open", 2), ("close", 2)], log
        )
        self.assertEqual(backend.concurrent_open_attempts, 0)
        self.assertTrue(all(not handle.is_open for handle in backend.handles))

    def test_unavailable_recovery_port_times_out(self):
        clock, backend, capture, log = self.run_capture(recovery_available=False)
        self.assertIsNotNone(backend.disconnect_at)
        elapsed = clock.now - backend.disconnect_at
        self.assertGreaterEqual(elapsed, 45.0)
        self.assertLess(elapsed, 46.0)
        recovery_attempts = [
            when
            for kind, when, port in backend.events
            if kind == "open_attempt" and port == "COM10"
        ]
        self.assertTrue(recovery_attempts)
        self.assertTrue(
            all(when < backend.disconnect_at + 45.0 for when in recovery_attempts)
        )
        self.assertEqual(len(backend.handles), 1)
        self.assertEqual(backend.concurrent_open_attempts, 0)
        self.assertFalse(capture.handle_open)
        self.assertIn("SERIAL_OPEN_RETRY=recovery port unavailable", log)
        self.assertIn("SERIAL_CAPTURE_ERROR=open_timeout", log)
        self.assertNotIn("RTC_CAPTURE_RECOVERED", log)

    def test_immediate_read_errors_do_not_extend_recovery_deadline(self):
        clock, backend, capture, log = self.run_capture(recovery_read_fails=True)
        self.assertIsNotNone(backend.disconnect_at)
        elapsed = clock.now - backend.disconnect_at
        self.assertGreaterEqual(elapsed, 45.0)
        self.assertLess(elapsed, 46.0)
        self.assertGreater(len(backend.handles), 1)
        self.assertEqual(backend.concurrent_open_attempts, 0)
        self.assertTrue(all(not handle.is_open for handle in backend.handles))
        self.assertFalse(capture.handle_open)
        self.assertIn("SERIAL_READ_ERROR=recovery read failed immediately", log)
        self.assertIn("SERIAL_CAPTURE_ERROR=open_timeout", log)
        self.assertNotIn("RTC_CAPTURE_RECOVERED", log)


if __name__ == "__main__":
    unittest.main()
