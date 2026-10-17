#!/usr/bin/env python3
"""Unit checks for JOURNAL / JOURNAL_DUMP parsing (no hardware)."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_thermometer2_aether_full_vanilla as rv  # noqa: E402

SAMPLE = """
BOOT cycle=1 session=42 run_id=65537
JOURNAL cycle=1 run_id=65537 got_ip=1 select_ok=1 write_called=1 write_status=0 fail_stage=0 active_ms=12000
JOURNAL cycle=2 run_id=65538 got_ip=1 select_ok=1 write_called=1 write_status=0 fail_stage=0 active_ms=11000
JOURNAL_DUMP count=100
JOURNAL_DUMP cycle=1 run_id=65537 got_ip=1 select_ok=1 write_called=1 write_status=0 fail_stage=0 active_ms=12000
JOURNAL_DUMP cycle=2 run_id=65538 got_ip=1 select_ok=0 write_called=0 write_status=255 fail_stage=3 active_ms=5000
CAMPAIGN_DONE
"""


def main() -> int:
    merged = rv.parse_journal_merged(SAMPLE)
    assert merged[1]["run_id"] == 65537
    assert merged[2]["select_ok"] == 0
    assert rv.journal_field_status(merged.get(99)) == "UNKNOWN"
    assert rv.journal_field_status(merged[1]) != "UNKNOWN"
    rx = "AETHER_FULL_READY run_id=65537\nAETHER_FULL_READY run_id=65538\n"
    smoke = rv.parse_smoke_detailed(SAMPLE, rx, expected_cycles=2)
    assert smoke["expected_run_ids"] == [65537, 65538]
    assert smoke["full_rx_count"] == 2
    assert smoke["pass"] is True
    print("journal_parser_ok=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
