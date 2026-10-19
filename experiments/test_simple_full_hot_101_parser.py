"""Unit tests for HOT RTC serial parsing."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load_runner(hot_count: int = 100):
    import os

    os.environ["SIMPLE_FULL_HOT_COUNT"] = str(hot_count)
    name = "run_simple_full_hot_101"
    path = HERE / "run_simple_full_hot_101.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _row(n: int, *, enc_c=1, enc_ok=1, snd_c=1, snd=1, err=0, st="OK") -> str:
    return (
        f"HOT_RTC_ROW n={n} got_ip=1 encode_called={enc_c} encode_ok={enc_ok} "
        f"nonce=1 sendto_called={snd_c} sendto={snd} err={err} state={st}"
    )


def _block(hot_count: int, run_id: int = 1) -> str:
    last = 1 + hot_count
    lines = [
        f"HOT_RTC_TABLE_BEGIN run_id={run_id} planned={hot_count} started={hot_count} "
        f"next={last + 1} finish=SCENARIO_DONE table=OK",
    ]
    for n in range(2, last + 1):
        lines.append(_row(n))
    lines.append("HOT_RTC_TABLE_END")
    return "\n".join(lines)


def test_empty_serial_attempts_unknown():
    mod = _load_runner(100)
    fw = mod.parse_firmware_serial("")
    assert fw["hot_rtc_table_complete"] is False
    assert mod.infer_actual_attempts(fw) == "UNKNOWN"


def test_partial_dump_not_complete():
    mod = _load_runner(5)
    text = _block(5)[: _block(5).index("HOT_RTC_TABLE_END")]
    fw = mod.parse_firmware_serial(text)
    assert fw["hot_rtc_table_complete"] is False


def test_full_dump_accepted():
    mod = _load_runner(5)
    fw = mod.parse_firmware_serial(_block(5))
    assert fw["hot_rtc_table_complete"] is True
    assert len(fw["hot_rtc_rows"]) == 5


def test_repeat_dump_no_double_count():
    mod = _load_runner(5)
    text = _block(5) + "\n" + _block(5)
    fw = mod.parse_firmware_serial(text)
    assert fw["hot_rtc_table_complete"] is True
    assert fw["hot_rtc_dump_blocks"] == 2
    assert len(fw["hot_rtc_rows"]) == 5


def test_wifi_fail_before_encode():
    mod = _load_runner(5)
    text = (
        "HOT_RTC_TABLE_BEGIN run_id=2 planned=5 started=1 next=3 "
        "finish=SCENARIO_FAIL:hot_send table=OK\n"
        + _row(2, enc_c=0, enc_ok=0, snd_c=0, snd=0, err=1, st="FAIL")
        + "\n"
        + "\n".join(_row(n, st="NOT_RUN") for n in range(3, 7))
        + "\nHOT_RTC_TABLE_END"
    )
    fw = mod.parse_firmware_serial(text)
    assert fw["hot_rtc_rows"][2]["encode_called"] == 0
    a = mod.analyze_hot_rtc(fw, [1, 2], test_sender=None)
    assert a is not None
    assert a["wifi_failures"] == 1


def test_sendto_ok_no_rx():
    mod = _load_runner(5)
    fw = mod.parse_firmware_serial(_block(5))
    a = mod.analyze_hot_rtc(fw, [1], test_sender=None)
    assert a is not None
    assert 2 in a["sendto_ok_but_not_received"]
