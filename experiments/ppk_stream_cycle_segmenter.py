"""Streaming PPK wake/sleep cycle detector for short-period UDP firmware runs.

Boundaries use block-mean envelope + hysteresis + minimum stable duration.
Energy integration must use full-rate raw samples (done by caller on index ranges).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class Phase(Enum):
    IGNORE_COLD_BOOT = auto()
    WAIT_WAKE = auto()
    IN_WAKE = auto()


@dataclass
class CycleWindow:
    sequence: int  # 0 = warmup, 1..100 measured
    wake_start_sample: int
    wake_end_sample: int
    wake_start_s: float
    wake_end_s: float
    max_gap_samples: int = 0


@dataclass
class SegmenterConfig:
    fs_hz: float = 100_000.0
    block_samples: int = 1000  # 10 ms @ 100 kHz
    cold_boot_ignore_s: float = 60.0
    wake_sample_ua: float = 3000.0
    sleep_sample_ua: float = 2000.0
    wake_frac: float = 0.85
    sleep_frac: float = 0.82
    wake_stable_blocks: int = 2  # 20 ms
    sleep_stable_blocks: int = 20  # 200 ms
    max_wake_s: float = 9.0
    max_measured: int = 100
    gap_invalid_samples: int = 500  # 5 ms missing @ 100 kHz


@dataclass
class StreamingCycleSegmenter:
    cfg: SegmenterConfig
    on_window: Callable[[CycleWindow], None] | None = None

    phase: Phase = Phase.IGNORE_COLD_BOOT
    total_samples: int = 0
    block_buf: list[float] = field(default_factory=list)
    block_index: int = 0

    high_run: int = 0
    low_run: int = 0

    wake_start_sample: int = 0
    wake_start_s: float = 0.0
    in_wake: bool = False

    windows_closed: list[CycleWindow] = field(default_factory=list)
    next_sequence: int = 0  # 0 warmup, then 1..100
    max_gap_in_wake: int = 0
    last_sample_index: int = -1

    def _block_time_s(self) -> float:
        return (self.block_index * self.cfg.block_samples) / self.cfg.fs_hz

    def _emit_window(self, end_sample: int, end_s: float) -> None:
        if self.wake_start_sample >= end_sample:
            return
        seq = self.next_sequence
        if seq > self.cfg.max_measured:
            return
        w = CycleWindow(
            sequence=seq,
            wake_start_sample=self.wake_start_sample,
            wake_end_sample=end_sample,
            wake_start_s=self.wake_start_s,
            wake_end_s=end_s,
            max_gap_samples=self.max_gap_in_wake,
        )
        self.windows_closed.append(w)
        if self.on_window:
            self.on_window(w)
        self.next_sequence = min(seq + 1, self.cfg.max_measured + 1)

    def _block_fracs(self, block: list[float]) -> tuple[float, float]:
        if not block:
            return 0.0, 0.0
        cfg = self.cfg
        arr = block  # len ~1000
        n = len(arr)
        hi = sum(1 for v in arr if v >= cfg.wake_sample_ua)
        lo = sum(1 for v in arr if v <= cfg.sleep_sample_ua)
        return hi / n, lo / n

    def _process_block(self, block: list[float]) -> None:
        t_block_end = self._block_time_s()
        if self.phase == Phase.IGNORE_COLD_BOOT:
            if t_block_end >= self.cfg.cold_boot_ignore_s:
                self.phase = Phase.WAIT_WAKE
            return

        hi_frac, lo_frac = self._block_fracs(block)
        if hi_frac >= self.cfg.wake_frac:
            self.high_run += 1
            self.low_run = 0
        elif lo_frac >= self.cfg.sleep_frac:
            self.low_run += 1
            self.high_run = 0
        else:
            self.high_run = 0

        if self.phase == Phase.WAIT_WAKE:
            if self.high_run >= self.cfg.wake_stable_blocks:
                self.phase = Phase.IN_WAKE
                self.in_wake = True
                start_sample = max(
                    0,
                    self.total_samples - self.cfg.block_samples * self.cfg.wake_stable_blocks,
                )
                self.wake_start_sample = start_sample
                self.wake_start_s = start_sample / self.cfg.fs_hz
                self.max_gap_in_wake = 0
            return

        if self.phase == Phase.IN_WAKE:
            now_s = self.total_samples / self.cfg.fs_hz
            force = (now_s - self.wake_start_s) >= self.cfg.max_wake_s
            if force or self.low_run >= self.cfg.sleep_stable_blocks:
                end_sample = self.total_samples
                end_s = end_sample / self.cfg.fs_hz
                self._emit_window(end_sample, end_s)
                self.phase = Phase.WAIT_WAKE
                self.in_wake = False
                self.high_run = 0
                self.low_run = 0

    def feed_chunk(self, uA_values) -> None:
        cfg = self.cfg
        for u in uA_values:
            ua = abs(float(u))
            if self.last_sample_index >= 0:
                gap = self.total_samples - self.last_sample_index - 1
                if gap > 1 and self.in_wake:
                    self.max_gap_in_wake = max(self.max_gap_in_wake, gap)
            self.last_sample_index = self.total_samples

            self.block_buf.append(ua)
            self.total_samples += 1
            if len(self.block_buf) >= cfg.block_samples:
                self.block_index += 1
                self._process_block(self.block_buf)
                self.block_buf = []

    def finalize(self) -> None:
        if self.phase == Phase.IN_WAKE and self.next_sequence <= self.cfg.max_measured:
            end_sample = self.total_samples
            self._emit_window(end_sample, end_sample / self.cfg.fs_hz)

    @property
    def measured_windows(self) -> int:
        return sum(1 for w in self.windows_closed if 1 <= w.sequence <= self.cfg.max_measured)

    @property
    def total_windows_including_warmup(self) -> int:
        return len(self.windows_closed)
