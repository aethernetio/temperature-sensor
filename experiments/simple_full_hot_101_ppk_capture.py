"""Continuous PPK2 raw capture for simple_full_hot_101 (single PPK owner)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

FS_HZ = 100_000.0
DT_S = 1.0 / FS_HZ


@dataclass
class PpkEvent:
    name: str
    t_wall: float
    sample_index: int = 0


@dataclass
class SimpleFullHotPpkCapture:
    ppk: Any
    raw_bin: Path
    voltage_mv: int = 3000
    voltage_source: str = "set_source_meter"
    events: list[PpkEvent] = field(default_factory=list)
    sample_count: int = 0
    read_error: str | None = None
    write_error: str | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _bin_f: Any = None
    _active: bool = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        self.raw_bin.parent.mkdir(parents=True, exist_ok=True)
        self._bin_f = open(self.raw_bin, "wb")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._active = True

    def mark_cold_boot(self) -> None:
        self.events.append(PpkEvent("cold_boot", time.time(), self.sample_count))

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._bin_f is not None:
            self._bin_f.close()
            self._bin_f = None
        self._active = False
        meta = {
            "sample_count": self.sample_count,
            "fs_hz": FS_HZ,
            "voltage_mv": self.voltage_mv,
            "stopped_at": datetime.now(timezone.utc).isoformat(),
            "read_error": self.read_error,
            "write_error": self.write_error,
        }
        if self.raw_bin.is_file():
            self.raw_bin.with_suffix(".json").write_text(
                __import__("json").dumps(meta, indent=2), encoding="utf-8"
            )
        return meta

    def _run(self) -> None:
        api = self.ppk.ppk
        api.start_measuring()
        try:
            while not self._stop.is_set():
                try:
                    raw = api.get_data()
                except Exception as e:
                    self.read_error = str(e)
                    break
                if not raw:
                    time.sleep(0.01)
                    continue
                chunk, _ = api.get_samples(raw)
                if not chunk:
                    continue
                arr = np.asarray(chunk, dtype=np.float32)
                try:
                    arr.tofile(self._bin_f)
                except Exception as e:
                    self.write_error = str(e)
                    break
                self.sample_count += int(arr.size)
        finally:
            try:
                api.stop_measuring()
            except Exception as e:
                if self.read_error is None:
                    self.read_error = str(e)


def analyze_power_capture(
    *,
    meta: dict[str, Any],
    events: list[PpkEvent],
    raw_bin: Path,
    voltage_mv: int,
    hot_count: int,
) -> dict[str, Any]:
    del events, hot_count, voltage_mv
    sample_count = int(meta.get("sample_count") or 0)
    read_error = meta.get("read_error")
    write_error = meta.get("write_error")
    expected_bytes = sample_count * 4
    actual_bytes = raw_bin.stat().st_size if raw_bin.is_file() else 0
    file_bytes_consistent = (
        sample_count > 0 and actual_bytes == expected_bytes and expected_bytes > 0
    )
    if read_error or write_error:
        status = "FAILED"
    elif sample_count == 0:
        status = "EMPTY"
    else:
        status = "UNKNOWN"
    out: dict[str, Any] = {
        "status": status,
        "continuity": "UNKNOWN",
        "sample_count": sample_count,
        "expected_bytes": expected_bytes,
        "actual_bytes": actual_bytes,
        "file_bytes_consistent": file_bytes_consistent,
        "read_error": read_error,
        "write_error": write_error,
    }
    if raw_bin.is_file():
        out["raw_bin"] = str(raw_bin)
    return out


def classify_power_capture_status(power: dict[str, Any] | None) -> str:
    if not power:
        return "NOT_RUN"
    return str(power.get("status") or "UNKNOWN")
