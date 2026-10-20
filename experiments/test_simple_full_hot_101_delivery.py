"""Delivery evaluation tests for simple_full_hot_101 runner."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def _load_runner():
    stubs = {
        name: types.ModuleType(name)
        for name in (
            "run_thermometer2_sleep_bisect",
            "run_thermometer2_aether_full_vanilla",
        )
    }
    stubs["run_thermometer2_sleep_bisect"].PpkSession = object
    name = "run_simple_full_hot_101"
    path = HERE / "run_simple_full_hot_101.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules.update(stubs)
    with patch.object(Path, "is_file", return_value=False):
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def test_full_delivery_pass():
    mod = _load_runner()
    nums = list(range(1, mod.LAST_MESSAGE + 1))
    ev = mod.evaluate(nums)
    assert ev["pass"] is True
    assert mod.classify_delivery(ev) == "PASS"


def test_missing_hot_two_not_pass():
    mod = _load_runner()
    nums = [1] + list(range(3, mod.LAST_MESSAGE + 1))
    ev = mod.evaluate(nums)
    assert 2 in ev["missing_numbers"]
    assert ev["pass"] is False


def test_missing_full_not_pass():
    mod = _load_runner()
    nums = list(range(2, mod.LAST_MESSAGE + 1))
    ev = mod.evaluate(nums)
    assert ev["FULL_RX"] == "0/1"
    assert ev["pass"] is False


def test_duplicate_not_pass():
    mod = _load_runner()
    nums = list(range(1, mod.LAST_MESSAGE + 1)) + [50]
    ev = mod.evaluate(nums)
    assert 50 in ev["duplicates"]
    assert ev["pass"] is False


def test_out_of_range_does_not_fill_gap():
    mod = _load_runner()
    nums = [1] + list(range(3, mod.LAST_MESSAGE + 1)) + [102]
    ev = mod.evaluate(nums)
    assert 2 in ev["missing_numbers"]
    assert 102 in ev["out_of_range_numbers"]
    assert ev["pass"] is False


def test_aborted_never_test_pass():
    mod = _load_runner()
    state = {"pass": True, "run_status": "ABORTED", "runner_interrupted": True}
    mod.finalize_test_status(state)
    assert state["TEST"] == "ABORTED"


def test_ppk_never_complete_without_continuity_check():
    from simple_full_hot_101_ppk_capture import analyze_power_capture
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp.write(b"\0" * 400)
        path = Path(tmp.name)
    try:
        out = analyze_power_capture(
            meta={"sample_count": 100, "read_error": None, "write_error": None},
            events=[],
            raw_bin=path,
            voltage_mv=3000,
            hot_count=100,
        )
        assert out["status"] == "UNKNOWN"
        assert out["continuity"] == "UNKNOWN"
        assert out["file_bytes_consistent"] is True
    finally:
        path.unlink(missing_ok=True)
