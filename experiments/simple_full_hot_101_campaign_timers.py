"""Campaign phase timers for simple_full_hot_101 (monotonic, testable)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


class MonotonicClock(Protocol):
    def now(self) -> float: ...


class SystemMonotonicClock:
    def now(self) -> float:
        return time.monotonic()


@dataclass
class CampaignTimerConfig:
    usb_startup_timeout_s: float
    bootstrap_extra_timeout_s: float
    full_active_timeout_s: float
    hot_cycle_timeout_s: float


@dataclass
class CampaignTimerState:
    campaign_start: float
    usb_last_progress: float
    bootstrap_begin: float | None = None
    full_begin: float | None = None
    hot_begin: float | None = None
    seen_rx: set[int] = field(default_factory=set)
    last_unique_rx: float | None = None

    def note_unique_rx(self, nums: list[int], *, now: float) -> bool:
        """True if a new unique message number appeared (expected sender already filtered)."""
        progress = False
        for n in nums:
            if n not in self.seen_rx:
                self.seen_rx.add(n)
                self.last_unique_rx = now
                progress = True
        return progress

    def note_usb_progress(self, *, now: float) -> None:
        self.usb_last_progress = now

    def mark_bootstrap_begin(self, *, now: float) -> None:
        if self.bootstrap_begin is None:
            self.bootstrap_begin = now

    def mark_full_begin(self, *, now: float) -> None:
        if self.full_begin is None:
            self.full_begin = now

    def mark_hot_begin(self, *, now: float) -> None:
        if self.hot_begin is None:
            self.hot_begin = now


@dataclass
class DeadlineResult:
    hit: bool = False
    error: str | None = None
    phase_tag: str | None = None


def evaluate_deadlines(
    *,
    phase: str,
    cfg: CampaignTimerConfig,
    timers: CampaignTimerState,
    now: float,
    ser_text: str,
    fw: dict,
    last_rx_num: int,
    last_message: int,
    hot_lane_active: bool,
    full_begin_seen: bool,
) -> DeadlineResult:
    """Phase-specific deadlines; HOT timer starts at hot_begin, not campaign/bootstrap."""
    out = DeadlineResult()
    if full_begin_seen and timers.full_begin is None:
        timers.mark_full_begin(now=now)

    if timers.full_begin is None:
        usb_limit = cfg.usb_startup_timeout_s
        if "BOOTSTRAP_BEGIN" in ser_text and not fw.get("bootstrap_done"):
            timers.mark_bootstrap_begin(now=now)
            usb_limit += cfg.bootstrap_extra_timeout_s
        elif (
            fw.get("bootstrap_done")
            or "PREPARED_EXPORT_RESULT=ok" in ser_text
            or last_rx_num >= 1
        ):
            return out
        if now - timers.usb_last_progress > usb_limit:
            out.hit = True
            out.error = "usb_startup_timeout_no_full_begin"
            out.phase_tag = "USB_TIMEOUT"
        return out

    if phase == "FULL" and timers.full_begin is not None:
        if now - timers.full_begin > cfg.full_active_timeout_s:
            out.hit = True
            out.error = "full_phase_timeout"
            out.phase_tag = "FULL_TIMEOUT"
        return out

    if (
        phase == "HOT"
        and timers.hot_begin is not None
        and last_rx_num < last_message
        and not fw.get("scenario_done")
    ):
        anchor = timers.last_unique_rx if timers.last_unique_rx is not None else timers.hot_begin
        if now - anchor > cfg.hot_cycle_timeout_s:
            out.hit = True
            out.error = "hot_cycle_timeout"
            out.phase_tag = "HOT_TIMEOUT"
    return out


def smoke_overall_deadline_s(cfg: CampaignTimerConfig, hot_count: int) -> float:
    """Conservative wall budget for runner + RTC tail (monotonic loop + real sleep)."""
    return (
        cfg.usb_startup_timeout_s
        + cfg.bootstrap_extra_timeout_s
        + cfg.full_active_timeout_s
        + hot_count * cfg.hot_cycle_timeout_s
        + 30.0  # TAIL_DELIVERY
        + 240.0  # RTC repeat wait
        + 120.0  # build/flash slack not included — shell must add build time separately
    )
