"""Unit tests for campaign timer logic (monotonic, controlled clock)."""
from __future__ import annotations

import unittest

from simple_full_hot_101_campaign_timers import (
    CampaignTimerConfig,
    CampaignTimerState,
    evaluate_deadlines,
)


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class CampaignTimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = CampaignTimerConfig(
            usb_startup_timeout_s=52.0,
            bootstrap_extra_timeout_s=180.0,
            full_active_timeout_s=300.0,
            hot_cycle_timeout_s=120.0,
        )

    def test_bootstrap_long_no_hot_timeout(self) -> None:
        clock = FakeClock(0.0)
        timers = CampaignTimerState(
            campaign_start=clock.now(), usb_last_progress=clock.now()
        )
        fw = {"bootstrap_done": False}
        ser = "BOOTSTRAP_BEGIN\n"
        for _ in range(100):
            clock.advance(1.0)
            dl = evaluate_deadlines(
                phase="USB_STARTUP",
                cfg=self.cfg,
                timers=timers,
                now=clock.now(),
                ser_text=ser,
                fw=fw,
                last_rx_num=0,
                last_message=6,
                hot_lane_active=False,
                full_begin_seen=False,
            )
            self.assertFalse(dl.hit)
        self.assertIsNotNone(timers.bootstrap_begin)

    def test_new_unique_rx_updates_progress(self) -> None:
        clock = FakeClock(100.0)
        timers = CampaignTimerState(
            campaign_start=clock.now(), usb_last_progress=clock.now()
        )
        self.assertTrue(timers.note_unique_rx([1], now=clock.now()))
        t1 = timers.last_unique_rx
        clock.advance(10.0)
        self.assertFalse(timers.note_unique_rx([1], now=clock.now()))
        self.assertEqual(timers.last_unique_rx, t1)
        self.assertTrue(timers.note_unique_rx([1, 2], now=clock.now()))
        self.assertGreater(timers.last_unique_rx, t1)

    def test_hot_timeout_uses_hot_begin_not_campaign(self) -> None:
        clock = FakeClock(0.0)
        timers = CampaignTimerState(
            campaign_start=clock.now(), usb_last_progress=clock.now()
        )
        timers.mark_full_begin(now=0.0)
        timers.mark_hot_begin(now=1000.0)
        fw = {"scenario_done": False}
        clock.t = 1000.0 + 119.0
        dl = evaluate_deadlines(
            phase="HOT",
            cfg=self.cfg,
            timers=timers,
            now=clock.now(),
            ser_text="",
            fw=fw,
            last_rx_num=2,
            last_message=6,
            hot_lane_active=True,
            full_begin_seen=True,
        )
        self.assertFalse(dl.hit)
        clock.t = 1000.0 + 121.0
        dl = evaluate_deadlines(
            phase="HOT",
            cfg=self.cfg,
            timers=timers,
            now=clock.now(),
            ser_text="",
            fw=fw,
            last_rx_num=2,
            last_message=6,
            hot_lane_active=True,
            full_begin_seen=True,
        )
        self.assertTrue(dl.hit)
        self.assertEqual(dl.error, "hot_cycle_timeout")

    def test_hot_rx_resets_timeout_anchor(self) -> None:
        clock = FakeClock(0.0)
        timers = CampaignTimerState(
            campaign_start=clock.now(), usb_last_progress=clock.now()
        )
        timers.mark_full_begin(now=0.0)
        timers.mark_hot_begin(now=0.0)
        timers.note_unique_rx([1, 2], now=50.0)
        clock.t = 50.0 + 119.0
        fw = {"scenario_done": False}
        dl = evaluate_deadlines(
            phase="HOT",
            cfg=self.cfg,
            timers=timers,
            now=clock.now(),
            ser_text="",
            fw=fw,
            last_rx_num=2,
            last_message=6,
            hot_lane_active=True,
            full_begin_seen=True,
        )
        self.assertFalse(dl.hit)

    def test_no_rx_progress_times_out_in_hot(self) -> None:
        clock = FakeClock(0.0)
        timers = CampaignTimerState(
            campaign_start=clock.now(), usb_last_progress=clock.now()
        )
        timers.mark_full_begin(now=0.0)
        timers.mark_hot_begin(now=10.0)
        fw = {"scenario_done": False}
        clock.t = 10.0 + 121.0
        dl = evaluate_deadlines(
            phase="HOT",
            cfg=self.cfg,
            timers=timers,
            now=clock.now(),
            ser_text="",
            fw=fw,
            last_rx_num=1,
            last_message=6,
            hot_lane_active=True,
            full_begin_seen=True,
        )
        self.assertTrue(dl.hit)
        self.assertEqual(dl.error, "hot_cycle_timeout")

    def test_full_rx_before_hot_does_not_shorten_hot_deadline(self) -> None:
        timers = CampaignTimerState(campaign_start=0.0, usb_last_progress=0.0)
        timers.mark_full_begin(now=0.0)
        timers.note_unique_rx([1], now=100.0)
        timers.mark_hot_begin(now=300.0)
        for now, expected_hit in ((301.0, False), (420.0, False), (421.0, True)):
            with self.subTest(now=now):
                dl = evaluate_deadlines(
                    phase="HOT",
                    cfg=self.cfg,
                    timers=timers,
                    now=now,
                    ser_text="",
                    fw={"scenario_done": False},
                    last_rx_num=1,
                    last_message=6,
                    hot_lane_active=True,
                    full_begin_seen=True,
                )
                self.assertEqual(dl.hit, expected_hit)
                if expected_hit:
                    self.assertEqual(dl.error, "hot_cycle_timeout")

    def test_hot_timeout_without_captured_full_begin(self) -> None:
        timers = CampaignTimerState(campaign_start=0.0, usb_last_progress=0.0)
        timers.note_unique_rx([1, 3], now=100.0)
        timers.mark_hot_begin(now=100.0)
        for now, expected_hit in ((220.0, False), (221.0, True)):
            with self.subTest(now=now):
                dl = evaluate_deadlines(
                    phase="HOT",
                    cfg=self.cfg,
                    timers=timers,
                    now=now,
                    ser_text="",
                    fw={"bootstrap_done": True, "scenario_done": False},
                    last_rx_num=3,
                    last_message=6,
                    hot_lane_active=True,
                    full_begin_seen=False,
                )
                self.assertEqual(dl.hit, expected_hit)
                self.assertIsNone(timers.full_begin)
                if expected_hit:
                    self.assertEqual(dl.error, "hot_cycle_timeout")
                    self.assertEqual(dl.phase_tag, "HOT_TIMEOUT")

    def test_full_timeout_not_bootstrap_usb_limit(self) -> None:
        clock = FakeClock(0.0)
        timers = CampaignTimerState(
            campaign_start=clock.now(), usb_last_progress=clock.now()
        )
        timers.mark_bootstrap_begin(now=0.0)
        timers.mark_full_begin(now=200.0)
        fw = {"bootstrap_done": True}
        ser = "BOOTSTRAP_BEGIN\nPREPARED_EXPORT_RESULT=ok\n"
        clock.t = 200.0 + 299.0
        dl = evaluate_deadlines(
            phase="FULL",
            cfg=self.cfg,
            timers=timers,
            now=clock.now(),
            ser_text=ser,
            fw=fw,
            last_rx_num=0,
            last_message=6,
            hot_lane_active=False,
            full_begin_seen=True,
        )
        self.assertFalse(dl.hit)
        clock.t = 200.0 + 301.0
        dl = evaluate_deadlines(
            phase="FULL",
            cfg=self.cfg,
            timers=timers,
            now=clock.now(),
            ser_text=ser,
            fw=fw,
            last_rx_num=0,
            last_message=6,
            hot_lane_active=False,
            full_begin_seen=True,
        )
        self.assertTrue(dl.hit)
        self.assertEqual(dl.error, "full_phase_timeout")


if __name__ == "__main__":
    unittest.main()
