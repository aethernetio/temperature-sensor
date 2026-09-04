#!/usr/bin/env python3
"""FULL vs HOT 1-minute energy comparison on chirkov only.

Test A: 10 FULL Æther sends @ 60 s start-to-start (continuous PPK integral).
Test B: 1 FULL prepare + 10 HOT @ 60 s; subtract prep FULL energy from total.

No sweeps, no aethernetio, no raw PPK CSV.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import run_prepared_power_factor_study as pf  # noqa: E402

prod_spec = importlib.util.spec_from_file_location(
    "prod", ROOT / "experiments" / "run_product_adaptive_probe.py"
)
prod = importlib.util.module_from_spec(prod_spec)
assert prod_spec.loader is not None
prod_spec.loader.exec_module(prod)

STARTING_SHA = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
).strip()

RUN_ID = time.strftime("%Y%m%d_%H%M%S")
AP = "chirkov"
VARIANT_ID = 400
ATTEMPTS = 10
PERIOD_MS = 60_000
PPK_VOLTAGE_MV = 3000
CR2_MAH = 800.0
SLEEP_UA = 8.0

BUILD_ROOT = ROOT / "build-full-vs-hot" / RUN_ID
RAW_DIR = ROOT / "experiments" / "power_modes_raw" / "full_vs_hot_1min"
RESULTS = ROOT / "experiments" / "power_factor_results"
REPORT_MD = ROOT / "experiments" / "PREPARED_FULL_VS_HOT_1MIN.md"
RESULT_JSON = RESULTS / "full_vs_hot_1min.json"
PROGRESS = ROOT / "experiments" / "full_vs_hot_1min_progress.log"
RX_LOG = ROOT / "experiments" / "full_vs_hot_1min_rx.log"

AETHER = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")
RX_BUILD = AETHER / "build-probe-receiver"
RX_EXE = RX_BUILD / "probe-receiver.exe"


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " FVSH " + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def ninja_build(bdir: Path) -> None:
    e = pf.camp.env()
    e["PATH"] = str(pf.camp.NINJA.parent) + ";" + e.get("PATH", "")
    r = subprocess.run(
        [str(pf.camp.NINJA), "-C", str(bdir)],
        cwd=ROOT,
        env=e,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        err = bdir / "ninja.err"
        err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        raise RuntimeError(f"ninja failed: {err}")


def cmake_common_args(bdir: Path, wifi: dict) -> list[str]:
    return [
        str(pf.camp.CMAKE),
        "-S",
        str(ROOT),
        "-B",
        str(bdir),
        "-G",
        "Ninja",
        "-DCMAKE_SUPPRESS_REGENERATION=ON",
        f"-DCMAKE_TOOLCHAIN_FILE={pf.camp.TOOLCHAIN.as_posix()}",
        "-DIDF_TARGET=esp32c6",
        f"-DCPM_aether-client-cpp_SOURCE={pf.camp.AETHER}",
        f"-DUSER_CONFIG={(ROOT / 'main' / 'user_config.h').as_posix()}",
        f"-DFS_INIT={pf.camp.FS_INIT.as_posix()}",
        f"-DSDKCONFIG={bdir.as_posix()}/sdkconfig",
        "-DAE_DISTILLATION=ON",
        "-DAE_FILTRATION=ON",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DAE_EXP_SILENT=1",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DWIFI_SSID={wifi['ssid']}",
        f"-DWIFI_PASSWORD={wifi['password']}",
        f"-DSERVICE_UID={pf.camp.SERVICE_UID}",
        f"-DPython3_EXECUTABLE={pf.camp.PY.as_posix()}",
        f"-DCMAKE_MAKE_PROGRAM={pf.camp.NINJA.as_posix()}",
        f"-DCMAKE_C_COMPILER={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-gcc.exe').as_posix()}",
        f"-DCMAKE_CXX_COMPILER={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-g++.exe').as_posix()}",
        f"-DCMAKE_OBJCOPY={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-objcopy.exe').as_posix()}",
        "-DBENCH_CLIENT_ID=reliability_full_v1",
    ]


def build_full_test() -> Path:
    bdir = BUILD_ROOT / "full10"
    if bdir.exists():
        shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True, exist_ok=True)
    pf.camp.BUILD = bdir
    pf.BUILD = bdir
    wifi = pf.camp.APS[AP]
    pf.camp.seed_usb_console_sdkconfig()
    # SKIP_VALIDATE for deep-sleep cadence (same production stack).
    pf.force_sdk_measured(VARIANT_ID)
    extra = {
        "AE_EXP_FULL_1MIN_10": "1",
        "AE_EXP_PREPARED_POWER_FACTOR": "",
        "AE_EXP_PREPARED_FINAL_1MIN_100": "",
        "AE_EXP_ENCODE_DURING_ASSOCIATION": "",
    }
    args = cmake_common_args(bdir, wifi)
    args.extend(
        [
            f"-DAE_FULL_1MIN_ATTEMPTS={ATTEMPTS}",
            f"-DAE_FULL_1MIN_PERIOD_MS={PERIOD_MS}",
        ]
    )
    args.extend(pf.clear_power_exp_flags(extra))
    for k, v in extra.items():
        args.append(f"-D{k}={v}")
    log(f"cmake Test A FULL -> {bdir}")
    pf.camp.clean_ninja_logs()
    r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    if r.returncode != 0:
        (bdir / "cmake.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("cmake Test A failed")
    pf.force_sdk_measured(VARIANT_ID)
    log("ninja Test A")
    ninja_build(bdir)
    return bdir


def build_hot_test() -> Path:
    bdir = BUILD_ROOT / "hot10"
    if bdir.exists():
        shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True, exist_ok=True)
    pf.camp.BUILD = bdir
    pf.BUILD = bdir
    wifi = pf.camp.APS[AP]
    pf.camp.seed_usb_console_sdkconfig()
    pf.force_sdk_measured(VARIANT_ID)
    extra = {
        "AE_EXP_PREPARED_POWER_FACTOR": "",
        "AE_EXP_PREPARED_FINAL_1MIN_100": "1",
        "AE_EXP_FULL_1MIN_10": "",
        "AE_EXP_ENCODE_DURING_ASSOCIATION": "1",
        "AE_EXP_ENCODE_OVERLAP_LEGACY_EARLY_SOCKET": "",
        "AE_EXP_ENCODE_OVERLAP_DIAG": "",
        "AE_POWER_BENCH_ARM_MS": "0",
    }
    args = cmake_common_args(bdir, wifi)
    args.extend(
        [
            f"-DAE_POWER_BENCH_VARIANT={VARIANT_ID}",
            "-DAE_POWER_BENCH_ARM_MS=0",
            f"-DAETHER_POWER_BENCH_HOT_ATTEMPTS={ATTEMPTS}",
            f"-DAETHER_POWER_BENCH_HOT_SLEEP_MS={PERIOD_MS}",
            f"-DAETHER_PREPARED_HOT_SLEEP_MS={PERIOD_MS}",
            f"-DAETHER_PREPARED_NONCE_RESERVE={ATTEMPTS + 10}",
        ]
    )
    args.extend(pf.clear_power_exp_flags(extra))
    for k, v in extra.items():
        args.append(f"-D{k}={v}")
    log(f"cmake Test B HOT -> {bdir}")
    pf.camp.clean_ninja_logs()
    r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    if r.returncode != 0:
        (bdir / "cmake.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("cmake Test B failed")
    pf.force_sdk_measured(VARIANT_ID)
    log("ninja Test B")
    ninja_build(bdir)
    return bdir


def start_ppk(checkpoint: Path) -> subprocess.Popen:
    ppk_py = pf.camp.PPK_PY
    script = ROOT / "experiments" / "ppk2_integrate_run.py"
    pid_file = ROOT / "experiments" / "ppk2_integrate.pid"
    if pid_file.exists():
        try:
            pid_file.unlink()
        except OSError:
            pass
    proc = subprocess.Popen(
        [
            str(ppk_py),
            str(script),
            "--voltage-mv",
            str(PPK_VOLTAGE_MV),
            "--checkpoint",
            str(checkpoint),
            "--checkpoint-every-sec",
            "5",
        ],
        cwd=str(ROOT / "experiments"),
    )
    time.sleep(3)
    if proc.poll() is not None:
        raise RuntimeError("ppk integrate failed to start")
    log(f"PPK pid={proc.pid} ckpt={checkpoint.name}")
    return proc


def stop_proc(proc: subprocess.Popen | None, grace: float = 8.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < grace:
        time.sleep(0.2)
    if proc.poll() is None:
        proc.kill()


def read_ppk(checkpoint: Path) -> dict:
    if not checkpoint.exists():
        return {}
    return json.loads(checkpoint.read_text(encoding="utf-8-sig"))


def parse_bench_rx(text: str, expected: int, *, require_arm: bool = False) -> dict:
    arm = "BENCH_ARM" in text
    seqs = [int(m.group(1)) for m in re.finditer(r"BENCH_DATA .* seq=(\d+)", text)]
    # For Test A FULL: variant=901. For HOT after ARM: any seq after arm.
    unique = len(set(seqs))
    return {
        "arm": arm,
        "rx_unique": unique,
        "rx_dup": len(seqs) - unique,
        "max_seq": max(seqs) if seqs else 0,
        "loss_pct": round(100.0 * max(0, expected - unique) / expected, 2),
        "ok_arm": (not require_arm) or arm,
    }


def wait_seqs(
    rx_log: Path,
    expected: int,
    timeout_s: float,
    *,
    require_arm: bool = False,
    on_arm=None,
) -> dict:
    t0 = time.time()
    arm_at = None
    last_at = None
    cadence = expected * (PERIOD_MS / 1000.0) + 180.0
    while time.time() - t0 < timeout_s:
        text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
        rx = parse_bench_rx(text, expected, require_arm=require_arm)
        if rx["arm"] and arm_at is None:
            arm_at = time.time()
            log("BENCH_ARM seen")
            if on_arm is not None:
                on_arm()
        if rx["max_seq"] >= expected:
            if last_at is None:
                last_at = time.time()
                log(f"seq{expected} seen unique={rx['rx_unique']}")
            elif time.time() - last_at >= 75:
                return rx
        start = arm_at if require_arm and arm_at else t0
        if (require_arm and arm_at and (time.time() - arm_at) >= cadence) or (
            not require_arm and (time.time() - t0) >= cadence
        ):
            log(f"cadence done unique={rx['rx_unique']}/{expected}")
            return rx
        time.sleep(2.0)
    text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
    return parse_bench_rx(text, expected, require_arm=require_arm)


def power_cycle_clear_rtc() -> None:
    prod.stop_ppk_hold()
    pf.camp.ppk_power_off()
    time.sleep(1.5)


def flash_erase(bdir: Path) -> str:
    pf.camp.BUILD = bdir
    return pf.camp.flash(erase=True)


def run_test_a() -> dict:
    log("=== TEST A: 10 FULL @ 60s ===")
    bdir = build_full_test()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ckpt = RAW_DIR / f"full10_{RUN_ID}_ppk.json"
    prod.kill_probe_receiver()
    time.sleep(0.5)
    if RX_LOG.exists():
        try:
            RX_LOG.unlink()
        except OSError:
            RX_LOG.write_text('', encoding='utf-8')
    if not RX_EXE.is_file():
        subprocess.run(["cmake", "--build", str(RX_BUILD), "--parallel"], check=False)
    prod.start_receiver(RX_LOG)

    power_cycle_clear_rtc()
    ppk = start_ppk(ckpt)
    try:
        port = flash_erase(bdir)
        log(f"flash ok {port}")
        rx = wait_seqs(RX_LOG, ATTEMPTS, timeout_s=1200, require_arm=False)
    finally:
        stop_proc(ppk)
        time.sleep(1)

    ppk_data = read_ppk(ckpt)
    energy = float(ppk_data.get("energy_J", 0))
    charge = float(ppk_data.get("charge_mAh", 0))
    elapsed = float(ppk_data.get("elapsed_s", 0))
    avg_mA = float(ppk_data.get("avg_current_mA", 0))
    avg_j = energy / ATTEMPTS if ATTEMPTS else 0.0
    row = {
        "mode": "FULL",
        "attempts": ATTEMPTS,
        "rx_unique": rx["rx_unique"],
        "loss_pct": rx["loss_pct"],
        "total_energy_J": energy,
        "total_charge_mAh": charge,
        "elapsed_s": elapsed,
        "avg_energy_J_per_msg": avg_j,
        "avg_current_mA": avg_mA,
        "ppk_checkpoint": str(ckpt),
    }
    log(
        f"FULL total_J={energy:.6f} avg_J={avg_j:.6f} "
        f"RX={rx['rx_unique']}/{ATTEMPTS}"
    )
    return row


def run_test_b() -> dict:
    log("=== TEST B: 1 FULL prepare + 10 HOT @ 60s ===")
    bdir = build_hot_test()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ckpt = RAW_DIR / f"hot10_{RUN_ID}_ppk.json"
    prep_snap = RAW_DIR / f"hot10_{RUN_ID}_prep_snapshot.json"
    prod.kill_probe_receiver()
    time.sleep(0.5)
    if RX_LOG.exists():
        try:
            RX_LOG.unlink()
        except OSError:
            RX_LOG.write_text('', encoding='utf-8')
    prod.start_receiver(RX_LOG)

    prep_energy = {"energy_J": 0.0}

    def on_arm() -> None:
        # Copy live checkpoint as prep FULL energy boundary.
        time.sleep(1.0)
        data = read_ppk(ckpt)
        prep_energy["energy_J"] = float(data.get("energy_J", 0))
        prep_snap.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log(f"HOT_PREP_FULL_ENERGY_J={prep_energy['energy_J']:.9f}")

    power_cycle_clear_rtc()
    ppk = start_ppk(ckpt)
    try:
        port = flash_erase(bdir)
        log(f"flash ok {port}")
        rx = wait_seqs(
            RX_LOG,
            ATTEMPTS,
            timeout_s=1500,
            require_arm=True,
            on_arm=on_arm,
        )
    finally:
        stop_proc(ppk)
        time.sleep(1)

    ppk_data = read_ppk(ckpt)
    run_total = float(ppk_data.get("energy_J", 0))
    prep = float(prep_energy["energy_J"])
    if prep <= 0 and prep_snap.exists():
        prep = float(read_ppk(prep_snap).get("energy_J", 0))
    hot10 = max(0.0, run_total - prep)
    avg_j = hot10 / ATTEMPTS if ATTEMPTS else 0.0
    # Average current for HOT-only period: energy/time of HOT segment.
    # Approximate HOT segment duration as attempts * 60s.
    hot_elapsed = ATTEMPTS * (PERIOD_MS / 1000.0)
    avg_mA = (avg_j / hot_elapsed / (PPK_VOLTAGE_MV / 1000.0) * 1000.0) if hot_elapsed else 0.0
    sleep_j = (PPK_VOLTAGE_MV / 1000.0) * (SLEEP_UA * 1e-6) * 60.0
    active_approx = max(0.0, avg_j - sleep_j)
    row = {
        "mode": "HOT",
        "attempts": ATTEMPTS,
        "rx_unique": rx["rx_unique"],
        "loss_pct": rx["loss_pct"],
        "prep_full_energy_J": prep,
        "combined_prep_plus_10hot_J": run_total,
        "prep_subtracted": True,
        "hot_10_total_energy_J": hot10,
        "avg_energy_J_per_msg": avg_j,
        "avg_current_mA": avg_mA,
        "approx_hot_active_minus_sleep_J": active_approx,
        "sleep_energy_per_min_J": sleep_j,
        "elapsed_s": float(ppk_data.get("elapsed_s", 0)),
        "ppk_checkpoint": str(ckpt),
        "prep_snapshot": str(prep_snap),
        "full_success": rx["arm"],
    }
    log(
        f"HOT prep_J={prep:.6f} run_J={run_total:.6f} hot10_J={hot10:.6f} "
        f"avg_J={avg_j:.6f} RX={rx['rx_unique']}/{ATTEMPTS}"
    )
    return row


def cr2_life(avg_mA: float) -> dict:
    if avg_mA <= 0:
        return {"hours": 0.0, "days": 0.0, "months": 0.0}
    hours = CR2_MAH / avg_mA
    days = hours / 24.0
    return {
        "hours": round(hours, 1),
        "days": round(days, 1),
        "months": round(days / 30.4375, 2),
    }


def write_report(full: dict, hot: dict) -> None:
    full_avg = full["avg_energy_J_per_msg"]
    hot_avg = hot["avg_energy_J_per_msg"]
    saving = full_avg - hot_avg
    ratio = (hot_avg / full_avg) if full_avg > 0 else 0.0
    pct = (1.0 - ratio) * 100.0 if full_avg > 0 else 0.0
    life = cr2_life(hot["avg_current_mA"])
    # If FULL refresh once per 100 HOT:
    full_extra_per_hot = full_avg / 100.0

    lines = [
        "# Prepared FULL vs HOT — chirkov 1-minute",
        "",
        f"Run id: `{RUN_ID}`",
        f"Starting SHA: `{STARTING_SHA}`",
        "",
        "Same cadence (≈60 s start-to-start). Sleep kept in both averages.",
        "HOT total = combined run − prep FULL energy.",
        "",
        "| mode | attempts | RX | loss | total energy J | avg energy/msg J | avg current mA |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| FULL | {full['attempts']} | {full['rx_unique']} | {full['loss_pct']}% | "
        f"{full['total_energy_J']:.6f} | {full_avg:.6f} | {full['avg_current_mA']:.6f} |",
        f"| HOT | {hot['attempts']} | {hot['rx_unique']} | {hot['loss_pct']}% | "
        f"{hot['hot_10_total_energy_J']:.6f} | {hot_avg:.6f} | {hot['avg_current_mA']:.6f} |",
        "",
        f"HOT_PREP_FULL_ENERGY_J={hot['prep_full_energy_J']:.9f}",
        f"HOT_RUN_TOTAL_ENERGY_J={hot['combined_prep_plus_10hot_J']:.9f}",
        f"HOT_10_TOTAL_ENERGY_J={hot['hot_10_total_energy_J']:.9f}",
        "",
        f"SAVING_J={saving:.6f}",
        f"HOT_VS_FULL={ratio:.4f}",
        f"SAVING_PERCENT={pct:.1f}",
        f"APPROX_HOT_ACTIVE_MINUS_SLEEP_J={hot['approx_hot_active_minus_sleep_J']:.6f}",
        f"(sleep@8µA≈{hot['sleep_energy_per_min_J']*1000:.2f} mJ/min)",
        "",
        f"CR2_HOT_LIFE={life['days']} d / {life['months']} mo "
        f"(from HOT avg current {hot['avg_current_mA']:.4f} mA)",
        f"FULL_REFRESH_EXTRA_PER_100_HOT_J={full_extra_per_hot:.6f}",
        "",
        "Old ~100–120 mJ HOT benchmarks compare to APPROX_HOT_ACTIVE_MINUS_SLEEP "
        "(order-of-magnitude sanity only).",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RESULTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": RUN_ID,
        "starting_sha": STARTING_SHA,
        "ap": AP,
        "period_ms": PERIOD_MS,
        "attempts": ATTEMPTS,
        "full": full,
        "hot": hot,
        "comparison": {
            "full_minus_hot_J": saving,
            "hot_vs_full_ratio": ratio,
            "saving_percent": pct,
            "cr2_hot_life": life,
            "full_refresh_extra_per_100_hot_J": full_extra_per_hot,
        },
    }
    RESULT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"report -> {REPORT_MD}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["all", "full", "hot"], default="all")
    args = ap.parse_args()
    log(f"starting_sha={STARTING_SHA}")

    full = None
    hot = None
    try:
        if args.phase in ("all", "full"):
            full = run_test_a()
        if args.phase in ("all", "hot"):
            hot = run_test_b()
    finally:
        prod.kill_probe_receiver()
        prod.stop_ppk_hold()

    if full and hot:
        write_report(full, hot)
    elif full or hot:
        RESULTS.mkdir(parents=True, exist_ok=True)
        RESULT_JSON.write_text(
            json.dumps({"run_id": RUN_ID, "full": full, "hot": hot}, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
