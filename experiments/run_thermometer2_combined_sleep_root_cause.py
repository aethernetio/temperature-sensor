#!/usr/bin/env python3
"""Thermometer 2 combined sleep root-cause campaign (raw contiguous µA).

Phases:
  zero   — PPK DUT OFF offset
  B1     — sleep-only B1_LED_ON_LOW corrected control
  P0     — combined Wi-Fi+UDP WITHOUT ApplyMinPowerDomains (broken baseline)
  P1     — combined WITH ApplyMinPowerDomains + RTC mem ON (candidate fix)
  confirm / final10 — after P1 passes

Sleep current: ALL samples in a contiguous window (no |I|<threshold filter).
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402
import run_thermometer2_udp_low_power_1min as lp  # noqa: E402
import run_prepared_power_factor_study as pf  # noqa: E402
import _diag_udp_rtc_index_flash as flash_mod  # noqa: E402
import ppk_contiguous_sleep as cs  # noqa: E402

RESULTS = HERE / "power_factor_results"
JSON_OUT = RESULTS / "thermometer2_combined_sleep_root_cause.json"
MD_OUT = HERE / "THERMOMETER2_COMBINED_SLEEP_ROOT_CAUSE.md"
RAW_DIR = HERE / "power_modes_raw" / "thermometer2_combined_sleep_root_cause"
OLD_MD = HERE / "THERMOMETER2_LOW_ENERGY_WIFI_PLUS_SLEEP.md"

UDP_HOST = "192.168.68.84"
UDP_PORT = 9000
VOLTAGE_MV = 3000
BUILD_B1 = ROOT / "build-sleep-bisect"
BUILD_P0 = ROOT / "build-udp-combined-p0"
BUILD_P1 = ROOT / "build-udp-combined-p1"
BUILD_FINAL = ROOT / "build-udp-combined-final"


def load() -> dict:
    if JSON_OUT.exists():
        return json.loads(JSON_OUT.read_text(encoding="utf-8"))
    return {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "voltage_mv": VOLTAGE_MV,
        "variants": [],
        "notes": [],
    }


def save(data: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data["updated_utc"] = datetime.now(timezone.utc).isoformat()
    branch, sha = sb.git_meta()
    data["branch"] = branch
    data["sha"] = sha
    JSON_OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    write_md(data)


def upsert(data: dict, row: dict) -> None:
    name = row["name"]
    data["variants"] = [v for v in data.get("variants", []) if v.get("name") != name]
    data["variants"].append(row)
    save(data)


def write_md(data: dict) -> None:
    lines = [
        "# Thermometer 2 — combined sleep root cause",
        "",
        f"- Branch: `{data.get('branch')}`",
        f"- SHA: `{data.get('sha')}`",
        f"- Voltage: {data.get('voltage_mv')} mV",
        f"- Sleep metric: **raw contiguous average** (no sample-current filter)",
        f"- Q/T cross-check required",
        "",
        "## Measurement bug (prior report)",
        "",
        "- Prior combined sleep ≈8.5 µA was **invalid** (`ua < 200` filtering).",
        "- Manual PPK contiguous window: **8.41 mA** = 343.16 mC / 40.79 s.",
        "- Marked SUPERSEDED in `THERMOMETER2_LOW_ENERGY_WIFI_PLUS_SLEEP.md`.",
        "",
        "| variant | operation before sleep | raw avg µA | charge mC | duration s | wake cause | verdict |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for v in data.get("variants", []):
        s = v.get("sleep") or {}
        lines.append(
            f"| {v.get('name')} | {v.get('operation')} | "
            f"{s.get('avg_uA')} | {s.get('charge_mC')} | {s.get('duration_s')} | "
            f"{v.get('wake_cause')} | {v.get('verdict')} |"
        )
    lines += ["", "## Notes", ""]
    for n in data.get("notes", []):
        lines.append(f"- {n}")
    if data.get("final_10x60"):
        lines += ["", "## Final 10x60", "", "```json", json.dumps(data["final_10x60"], indent=2), "```"]
    MD_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def patch_old_report(fix_sha: str) -> None:
    if not OLD_MD.exists():
        return
    text = OLD_MD.read_text(encoding="utf-8")
    banner = (
        "\n> **SUPERSEDED / MEASUREMENT BUG**\n"
        "> Previous sleep value ~8.5 µA was invalid due to sample-threshold filtering "
        "(`ua < 200`). Manual PPK contiguous window showed **~8.41 mA** "
        "(343.16 mC / 40.79 s). See `THERMOMETER2_COMBINED_SLEEP_ROOT_CAUSE.md`. "
        f"Fix work on `diag/thermometer2-combined-sleep-root-cause` (SHA evolving; "
        f"analyzer fix starts at `{fix_sha}`).\n"
    )
    if "SUPERSEDED / MEASUREMENT BUG" not in text:
        # Insert after title line
        parts = text.split("\n", 1)
        text = parts[0] + "\n" + banner + (parts[1] if len(parts) > 1 else "")
        OLD_MD.write_text(text, encoding="utf-8")


def measure_zero(ppk: sb.PpkSession) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ppk.set_off()
    time.sleep(2.0)
    csv = RAW_DIR / "ppk_zero.csv"
    # measure() asserts ON — call low-level path
    assert ppk.ppk is not None
    ppk.ppk.start_measuring()
    samples: list[tuple[float, float]] = []
    t0 = time.time()
    try:
        while time.time() - t0 < 8.0:
            raw = ppk.ppk.get_data()
            now = time.time()
            if raw:
                chunk, _ = ppk.ppk.get_samples(raw)
                for uA in chunk:
                    samples.append((now - t0, float(uA)))
            time.sleep(0.001)
    finally:
        ppk.ppk.stop_measuring()
    us = [u for _, u in samples]
    avg = statistics.fmean(us) if us else float("nan")
    return {"PPK_ZERO_AVG_UA": avg, "samples": len(us), "csv": str(csv)}


def wait_sleep_then_measure(
    ppk: sb.PpkSession,
    name: str,
    wait_s: float,
    measure_s: float,
) -> dict:
    print(f"=== WAIT {wait_s:.0f}s for deep sleep ({name}) ===", flush=True)
    time.sleep(wait_s)
    csv = RAW_DIR / f"{name}_sleep.csv"
    print(f"=== MEASURE contiguous {measure_s:.0f}s ({name}) ===", flush=True)
    meas = ppk.measure(measure_s, csv)
    raw = meas.get("raw_contiguous") or {}
    if not raw.get("q_over_t_ok", False):
        raise RuntimeError(f"Q/T fail for {name}: {raw}")
    # Hard physics: if charge implies mA, avg must not be µA
    if raw.get("duration_s", 0) >= 10 and raw.get("charge_mC", 0) >= 50 and raw.get("avg_uA", 0) < 100:
        raise RuntimeError(f"analyzer lie for {name}: {raw}")
    return meas


def build_combined(build_dir: Path, apply_min_pd: int, long_sleep: int) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    wifi = pf.camp.APS["chirkov"]
    b = build_dir.as_posix()
    defs = (
        f"-B '{b}' "
        f"-D AETHER_DIAG_UDP_LOW_POWER_1MIN_10=1 "
        f"-D AE_COMBINED_APPLY_MIN_PD={apply_min_pd} "
        f"-D AE_COMBINED_LONG_SLEEP_AFTER_FIRST={long_sleep} "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={wifi['ssid']} "
        f"-D WIFI_PASSWORD={wifi['password']} "
        f"-D AE_UDP_SERVER_HOST={UDP_HOST} "
        f"-D AE_UDP_SERVER_PORT={UDP_PORT} "
        f"-D AE_UDP_PRE_SETTLE_MS=50 "
        f"-D AE_UDP_POST_SEND_HOLD_MS=200"
    )
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} build
"""
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"build failed {build_dir} code={r.returncode}")
    sdk = build_dir / "sdkconfig"
    if sdk.exists():
        before = sdk.read_text(encoding="utf-8", errors="replace")
        flash_mod.force_sdk(sdk)
        text = sdk.read_text(encoding="utf-8", errors="replace")
        bset = pf._set_kconfig_bool
        text = bset(text, "CONFIG_ESP_CONSOLE_UART_DEFAULT", False)
        text = bset(text, "CONFIG_ESP_CONSOLE_NONE", True)
        sdk.write_text(text, encoding="utf-8")
        after = sdk.read_text(encoding="utf-8", errors="replace")
        if before != after:
            r2 = sb.run(sb.idf_cmd(f"idf.py {defs} build"), cwd=str(ROOT))
            if r2.returncode != 0:
                raise RuntimeError(f"rebuild failed {build_dir}")


def flash_with_ppk(ppk: sb.PpkSession, build_dir: Path) -> str:
    return sb.try_flash_with_retries(ppk, build_dir, cold_wait_s=40.0, attempts=5)


def start_udp() -> None:
    lp.kill_udp_9000()
    subprocess.Popen(
        [
            sys.executable,
            str(HERE / "udp_rtc_index_server.py"),
            "--host",
            "0.0.0.0",
            "--port",
            str(UDP_PORT),
        ],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)


def row_from_meas(
    name: str,
    operation: str,
    meas: dict,
    wake_cause: str,
    extra: dict | None = None,
) -> dict:
    raw = meas.get("raw_contiguous") or {}
    avg = float(raw.get("avg_uA", meas.get("sleep_avg_uA", float("nan"))))
    if avg <= 16:
        verdict = "TARGET"
    elif avg <= 25:
        verdict = "INTERMEDIATE"
    else:
        verdict = "FAIL"
    out = {
        "name": name,
        "operation": operation,
        "wake_cause": wake_cause,
        "sleep": raw,
        "verdict": verdict,
        "utc": datetime.now(timezone.utc).isoformat(),
        "csv": meas.get("csv"),
    }
    if extra:
        out.update(extra)
    return out


def phase_b1(ppk: sb.PpkSession, data: dict) -> None:
    print("=== BUILD B1_LED_ON_LOW ===", flush=True)
    sb.build_variant("B1_LED_ON_LOW", BUILD_B1)
    port = flash_with_ppk(ppk, BUILD_B1)
    # cold window 25s or flash window 8s; wait past enter sleep + 5s settle
    meas = wait_sleep_then_measure(ppk, "B1", wait_s=15.0, measure_s=40.0)
    upsert(
        data,
        row_from_meas(
            "B1_CORRECTED_CONTROL",
            "sleep-only B1_LED_ON_LOW",
            meas,
            "timer_expected",
            {"port": port},
        ),
    )


def phase_combined(
    ppk: sb.PpkSession,
    data: dict,
    name: str,
    build_dir: Path,
    apply_min_pd: int,
) -> None:
    print(f"=== BUILD {name} apply_min_pd={apply_min_pd} ===", flush=True)
    build_combined(build_dir, apply_min_pd=apply_min_pd, long_sleep=1)
    start_udp()
    port = flash_with_ppk(ppk, build_dir)
    # warmup + first measured send then 10min sleep; wait ~45s for wake path
    print("=== WAIT for first UDP + long sleep ===", flush=True)
    time.sleep(55.0)
    meas = wait_sleep_then_measure(ppk, name, wait_s=5.0, measure_s=40.0)
    # UDP check from live log
    log = HERE / "udp_rtc_index_server_live.log"
    rx = 0
    if log.exists():
        txt = log.read_text(encoding="utf-8", errors="replace")
        rx = txt.count("idx=")
    upsert(
        data,
        row_from_meas(
            name,
            f"UDP+teardown apply_min_pd={apply_min_pd}",
            meas,
            "timer_after_udp",
            {"port": port, "udp_log_idx_hits": rx},
        ),
    )


def phase_confirm(ppk: sb.PpkSession, data: dict, n: int = 3) -> None:
    for i in range(1, n + 1):
        name = f"CONFIRM_{i}"
        print(f"=== {name} ===", flush=True)
        start_udp()
        port = flash_with_ppk(ppk, BUILD_P1)
        time.sleep(55.0)
        meas = wait_sleep_then_measure(ppk, name, wait_s=5.0, measure_s=35.0)
        upsert(
            data,
            row_from_meas(
                name,
                "UDP+teardown+minPD",
                meas,
                "timer_after_udp",
                {"port": port},
            ),
        )
        avg = float((meas.get("raw_contiguous") or {}).get("avg_uA", 1e9))
        if avg > 25:
            data["notes"].append(f"{name} failed avg={avg}")
            save(data)
            raise RuntimeError(f"{name} sleep {avg} µA > 25")


def phase_final10(ppk: sb.PpkSession, data: dict) -> None:
    print("=== FINAL 10x60 build (minPD, normal 60s period) ===", flush=True)
    build_combined(BUILD_FINAL, apply_min_pd=1, long_sleep=0)
    start_udp()
    flash_with_ppk(ppk, BUILD_FINAL)
    # Capture ~11 minutes
    csv = RAW_DIR / "final10_capture.csv"
    capture_s = 8 + 10 * 60 + 30
    print(f"=== CAPTURE {capture_s}s ===", flush=True)
    meas = ppk.measure(capture_s, csv)
    # Re-analyze with contiguous sleep windows from CSV
    t, ua = cs.load_ua_csv(csv)
    # measure() CSV is downsampled timestamps — use them
    wakes = cs.find_wake_segments(t, ua)
    # drop first (power-on / warmup)
    wakes_m = wakes[1:11] if len(wakes) > 11 else wakes[1:]
    sleep_rows = []
    for win in cs.sleep_windows_between_wakes(wakes_m, float(t[-1]), guard_s=2.0, min_window_s=15.0):
        st = cs.contiguous_stats(t, ua, win[0], win[1])
        cs.assert_q_over_t(st)
        sleep_rows.append(st)
    # active charges
    active = []
    for a, b in wakes_m:
        m = (t >= a) & (t <= b)
        q = float(np.trapezoid(ua[m] * 1e-6, t[m])) * 1000.0
        active.append(q)
    avgs = [s["avg_uA"] for s in sleep_rows]
    data["final_10x60"] = {
        "wakes_detected": len(wakes),
        "measured_wakes": len(wakes_m),
        "active_mC": active,
        "active_mean_mC": statistics.mean(active) if active else None,
        "active_median_mC": statistics.median(active) if active else None,
        "sleep_windows": sleep_rows,
        "sleep_window_avg_uA": statistics.mean(avgs) if avgs else None,
        "sleep_window_worst_uA": max(avgs) if avgs else None,
        "total_charge_C": float(np.trapezoid(ua * 1e-6, t)),
        "csv": str(csv),
        "measure_meta": {k: meas[k] for k in ("samples", "avg_uA", "max_uA") if k in meas},
    }
    save(data)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        choices=["all", "zero", "b1", "p0", "p1", "confirm", "final10", "report"],
        default="all",
    )
    args = ap.parse_args()

    data = load()
    branch, sha = sb.git_meta()
    patch_old_report(sha)
    data["original"] = {
        "branch": "diag/thermometer2-lowenergy-wifi-plus-7ua-sleep",
        "sha": "6aaa6f3d4bd6f6e7a914097b7a412b898496abde",
    }
    save(data)

    if args.phase == "report":
        write_md(data)
        return 0

    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    try:
        if args.phase in ("all", "zero"):
            z = measure_zero(ppk)
            data["PPK_ZERO_AVG_UA"] = z["PPK_ZERO_AVG_UA"]
            data["notes"].append(f"PPK_ZERO_AVG_UA={z['PPK_ZERO_AVG_UA']:.3f}")
            save(data)
            ppk.set_on()

        if args.phase in ("all", "b1"):
            phase_b1(ppk, data)

        if args.phase in ("all", "p0"):
            phase_combined(ppk, data, "COMBINED_P0_BROKEN", BUILD_P0, apply_min_pd=0)

        if args.phase in ("all", "p1"):
            phase_combined(ppk, data, "COMBINED_P1_MIN_PD", BUILD_P1, apply_min_pd=1)

        # Auto-confirm if P1 target
        if args.phase == "all":
            p1 = next((v for v in data["variants"] if v["name"] == "COMBINED_P1_MIN_PD"), None)
            if p1 and p1.get("verdict") in ("TARGET", "INTERMEDIATE"):
                phase_confirm(ppk, data, 3)
                phase_final10(ppk, data)
            else:
                data["notes"].append("P1 did not reach <=25 µA; stop before confirm — continue bisect")
                save(data)

        if args.phase == "confirm":
            phase_confirm(ppk, data, 3)
        if args.phase == "final10":
            phase_final10(ppk, data)
    finally:
        try:
            ppk.close()
        except Exception:
            pass
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
