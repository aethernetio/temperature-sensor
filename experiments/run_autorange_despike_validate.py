#!/usr/bin/env python3
"""Validate B1 + combined sleep with PPK autorange despike metric."""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_thermometer2_combined_sleep_root_cause as camp  # noqa: E402
import run_thermometer2_sleep_bisect as sb  # noqa: E402


def main() -> int:
    raw = camp.RAW_DIR
    data = camp.load()
    ppk = sb.PpkSession(3000)
    ppk.open()
    try:
        print("=== B1 DESPIKE METRIC ===", flush=True)
        sb.build_variant("B1_LED_ON_LOW", camp.BUILD_B1)
        sb.try_flash_with_retries(ppk, camp.BUILD_B1, cold_wait_s=40.0, attempts=5)
        time.sleep(15)
        meas = ppk.measure(35.0, raw / "B1_AUTORANGE_DESPIKE_sleep.csv")
        print("B1", meas["raw_contiguous"], flush=True)
        camp.upsert(
            data,
            camp.row_from_meas(
                "B1_AUTORANGE_DESPIKE", "B1+autorange_despike", meas, "timer"
            ),
        )

        print("=== COMBINED P1 DESPIKE ===", flush=True)
        camp.build_combined(camp.BUILD_P1, apply_min_pd=1, long_sleep=1)
        camp.start_udp()
        sb.try_flash_with_retries(ppk, camp.BUILD_P1, cold_wait_s=40.0, attempts=5)
        time.sleep(60)
        meas = ppk.measure(35.0, raw / "COMBINED_P1_AUTORANGE_DESPIKE_sleep.csv")
        print("P1", meas["raw_contiguous"], flush=True)
        camp.upsert(
            data,
            camp.row_from_meas(
                "COMBINED_P1_AUTORANGE_DESPIKE",
                "UDP+minPD+autorange_despike",
                meas,
                "timer_after_udp",
            ),
        )

        avg = (meas.get("raw_contiguous") or {}).get("avg_uA", 1e9)
        print("P1_AVG", avg, flush=True)
        if avg <= 16:
            for i in range(1, 4):
                print(f"=== CONFIRM {i} ===", flush=True)
                camp.start_udp()
                sb.try_flash_with_retries(
                    ppk, camp.BUILD_P1, cold_wait_s=40.0, attempts=4
                )
                time.sleep(60)
                meas = ppk.measure(
                    30.0, raw / f"CONFIRM{i}_AUTORANGE_DESPIKE_sleep.csv"
                )
                print(f"CONFIRM{i}", meas["raw_contiguous"], flush=True)
                camp.upsert(
                    data,
                    camp.row_from_meas(
                        f"CONFIRM{i}_AUTORANGE_DESPIKE",
                        "UDP+minPD+despike",
                        meas,
                        "timer_after_udp",
                    ),
                )
                a = (meas.get("raw_contiguous") or {}).get("avg_uA", 1e9)
                if a > 25:
                    print("CONFIRM_FAIL", a, flush=True)
                    break
    finally:
        try:
            ppk.close()
        except Exception:
            pass
    print("DESPIKE_VALIDATE_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
