"""Unit tests for single-owner SerialCapture."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from simple_full_hot_101_serial import SerialCapture


class FakeBackend:
    def __init__(self) -> None:
        self.open_count = 0
        self.close_count = 0
        self.handles: list[object] = []
        self.read_fail_after = 0
        self._reads = 0

    def open(self, port: str):
        if any(h for h in self.handles if getattr(h, "open", False)):
            raise RuntimeError("second open forbidden")
        self.open_count += 1
        h = type("H", (), {"open": True, "port": port})()
        self.handles.append(h)
        return h

    def close(self, handle) -> None:
        self.close_count += 1
        handle.open = False

    def read(self, handle, size: int) -> bytes:
        self._reads += 1
        if self.read_fail_after and self._reads >= self.read_fail_after:
            handle.open = False
            raise OSError("device disconnected")
        time.sleep(0.05)
        return b"BOOT reset_reason=0 next_message=1\n"


def test_single_open_and_stop_closes(tmp_path: Path):
    log = tmp_path / "serial.log"
    be = FakeBackend()
    cap = SerialCapture(log, backend=be, port_hint="COM9")
    cap.start()
    time.sleep(0.3)
    assert be.open_count == 1
    cap.stop()
    assert be.close_count >= 1
    assert not cap.handle_open


def test_disconnect_rediscover(tmp_path: Path):
    log = tmp_path / "serial.log"
    be = FakeBackend()
    be.read_fail_after = 2
    ports = iter(["COM9", "COM10"])

    def discover():
        return next(ports, None)

    cap = SerialCapture(log, backend=be, discover=discover)
    cap.start()
    time.sleep(1.5)
    cap.stop()
    assert be.open_count >= 2
    assert be.close_count >= 2
    text = log.read_text(encoding="utf-8")
    assert "SERIAL_REDISCOVER" in text
