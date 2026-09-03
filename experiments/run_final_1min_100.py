#!/usr/bin/env python3
"""Final 1-minute cadence energy test: 1 FULL + 100 HOT per AP.

One PPK integral covers FULL prepare through HOT #100 final deep sleep.
No sweeps, no per-cycle energy stats, no timing telemetry.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import importlib.util

import run_prepared_power_factor_study as pf  # noqa: E402

prod_spec = importlib.util.spec_from_file_location(
    "prod", ROOT / "experiments" / "run_product_adaptive_probe.py"
)
prod = importlib.util.module_from_spec(prod_spec)
assert prod_spec.loader is not None
prod_spec.loader.exec_module(prod)

RUN_ID = time.strftime("%Y%m%d_%H%M%S")
BUILD_ROOT = ROOT / "build-final-1min" / RUN_ID
RESULTS_DIR = ROOT / "experiments" / "power_factor_results"
RAW_DIR = ROOT / "experiments" / "power_modes_raw" / "final_1min_100"
REPORT_MD = ROOT / "experiments" / "PREPARED_FINAL_1MIN_100_REPORT.md"
RESULT_JSON = RESULTS_DIR / "final_1min_100.json"
PROGRESS = ROOT / "experiments" / "final_1min_100_progress.log"

AETHER = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")
RX_BUILD = AETHER / "build-probe-receiver"
RX_EXE = RX_BUILD / "probe-receiver.exe"

VARIANT_ID = 400
K_HOT_ATTEMPTS = 100
K_HOT_SLEEP_MS = 60_000
K_ARM_MS = 0
PPK_VOLTAGE_MV = 3000
CR2_MAH = 800.0

RUN_TIMEOUT_S = 8000
IDLE_AFTER_SEQ100_S = 90
POLL_S = 2.0

APS_ORDER = ["chirkov", "aethernetio"]


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " FINAL " + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_dir(ap: str) -> Path:
    return BUILD_ROOT / ap


def cmake_configure(ap: str) -> None:
    bdir = build_dir(ap)
    bdir.mkdir(parents=True, exist_ok=True)
    pf.BUILD = bdir
    pf.CONFIGURED_MARK = bdir / "configured.txt"
    pf.camp.BUILD = bdir
    wifi = pf.camp.APS[ap]
    if bdir.exists() and not (bdir / "CMakeCache.txt").exists():
        import shutil

        shutil.rmtree(bdir, ignore_errors=True)
        bdir.mkdir(parents=True, exist_ok=True)
    pf.camp.seed_usb_console_sdkconfig()
    pf.force_sdk_measured(VARIANT_ID)
    extra = {
        "AE_EXP_PREPARED_POWER_FACTOR": "",
        "AE_EXP_PREPARED_FINAL_1MIN_100": "1",
        "BENCH_CLIENT_ID": "reliability_full_v1",
        "AE_POWER_BENCH_ARM_MS": str(K_ARM_MS),
    }
    args = [
        str(pf.camp.CMAKE),
        "-S",
        str(ROOT),
        "-B",
        str(bdir),
        "-G",
        "Ninja",
        "-DCMAKE_SUPPRESS_REGENERATION=ON",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
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
        f"-DAE_POWER_BENCH_VARIANT={VARIANT_ID}",
        f"-DAE_POWER_BENCH_ARM_MS={K_ARM_MS}",
        f"-DAETHER_POWER_BENCH_HOT_ATTEMPTS={K_HOT_ATTEMPTS}",
        f"-DAETHER_POWER_BENCH_HOT_SLEEP_MS={K_HOT_SLEEP_MS}",
        f"-DAETHER_PREPARED_HOT_SLEEP_SECONDS={K_HOT_SLEEP_MS // 1000}",
        f"-DAETHER_PREPARED_HOT_SLEEP_MS={K_HOT_SLEEP_MS}",
        f"-DAETHER_PREPARED_NONCE_RESERVE={K_HOT_ATTEMPTS + 10}",
    ]
    args.extend(pf.clear_power_exp_flags(extra))
    for k, v in extra.items():
        args.append(f"-D{k}={v}")
    log(f"cmake {ap} -> {bdir}")
    pf.camp.clean_ninja_logs()
    r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    if r.returncode != 0:
        err = bdir / "cmake.err"
        err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        raise RuntimeError(f"cmake failed: {err}")
    pf.force_sdk_measured(VARIANT_ID)


def ninja_build_local() -> None:
    log("ninja build")
    e = pf.camp.env()
    e["PATH"] = str(pf.camp.NINJA.parent) + ";" + e.get("PATH", "")
    r = subprocess.run(
        [str(pf.camp.NINJA), "-C", str(pf.camp.BUILD)],
        cwd=ROOT,
        env=e,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        err = pf.camp.BUILD / "ninja.err"
        err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        raise RuntimeError(f"ninja failed: {err}")


def build_firmware(ap: str) -> None:
    bdir = build_dir(ap)
    pf.BUILD = bdir
    pf.CONFIGURED_MARK = bdir / "configured.txt"
    pf.camp.BUILD = bdir
    want = f"{ap}:final100:v{VARIANT_ID}:r{K_HOT_ATTEMPTS + 10}"
    have = pf.CONFIGURED_MARK.read_text(encoding="utf-8").strip() if pf.CONFIGURED_MARK.exists() else ""
    bin_path = bdir / "temperature_sensor.bin"
    src_paths = [
        ROOT / "main" / "prepared_power_factor_bench.cpp",
        ROOT / "main" / "prepared_send" / "prepared_send.cpp",
    ]
    bin_fresh = bin_path.exists() and all(
        p.exists() and bin_path.stat().st_mtime >= p.stat().st_mtime for p in src_paths
    )
    if have != want:
        cmake_configure(ap)
        pf.CONFIGURED_MARK.write_text(want, encoding="utf-8")
    elif not bin_fresh:
        pf.force_sdk_measured(VARIANT_ID)
    if bin_fresh and have == want:
        log("build skipped (firmware up to date)")
        return
    ninja_build_local()


def start_receiver_log(rx_log: Path) -> None:
    if not RX_EXE.is_file():
        log("building probe_receiver")
        r = subprocess.run(
            ["cmake", "--build", str(RX_BUILD), "--parallel"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"probe_receiver build failed: {r.stderr}")
    prod.start_receiver(rx_log)


def start_ppk_integrate(checkpoint: Path) -> subprocess.Popen:
    ppk_py = pf.camp.PPK_PY
    script = ROOT / "experiments" / "ppk2_integrate_run.py"
    if not ppk_py.exists():
        raise RuntimeError(f"PPK venv missing: {ppk_py}")
    decimated = checkpoint.with_suffix(".decimated.csv")
    proc = subprocess.Popen(
        [
            str(ppk_py),
            str(script),
            "--voltage-mv",
            str(PPK_VOLTAGE_MV),
            "--checkpoint",
            str(checkpoint),
            "--checkpoint-every-sec",
            "10",
            "--decimated-out",
            str(decimated),
        ],
        cwd=str(ROOT / "experiments"),
    )
    time.sleep(3)
    if proc.poll() is not None:
        raise RuntimeError("ppk2_integrate_run exited immediately")
    log(f"PPK integrate pid={proc.pid}")
    return proc


def stop_proc(proc: subprocess.Popen | None, grace_s: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < grace_s:
        time.sleep(0.2)
    if proc.poll() is None:
        proc.kill()


def parse_rx_log(text: str) -> dict:
    bench_arm = "BENCH_ARM" in text
    seqs = [
        int(m.group(1))
        for m in re.finditer(r"BENCH_DATA .* seq=(\d+)", text)
    ]
    unique = len(set(seqs))
    dup = len(seqs) - unique
    missing = max(0, K_HOT_ATTEMPTS - unique)
    max_seq = max(seqs) if seqs else 0
    return {
        "full_success": bench_arm,
        "hot_attempts": K_HOT_ATTEMPTS,
        "rx_unique": unique,
        "rx_dup": dup,
        "rx_missing": missing,
        "max_seq": max_seq,
        "loss_pct": round(100.0 * missing / K_HOT_ATTEMPTS, 2),
    }


def wait_run_done(rx_log: Path, ppk_proc: subprocess.Popen) -> dict:
    t0 = time.time()
    last_seq100 = None
    while time.time() - t0 < RUN_TIMEOUT_S:
        text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
        rx = parse_rx_log(text)
        if rx["max_seq"] >= K_HOT_ATTEMPTS:
            if last_seq100 is None:
                last_seq100 = time.time()
                log(f"seq100 seen unique={rx['rx_unique']}")
            elif time.time() - last_seq100 >= IDLE_AFTER_SEQ100_S:
                log("idle after seq100 — run complete")
                return rx
        time.sleep(POLL_S)
    raise RuntimeError("run timeout")


def cr2_life(avg_mA: float) -> dict:
    if avg_mA <= 0:
        return {"days": 0.0, "months": 0.0}
    hours = CR2_MAH / avg_mA
    days = hours / 24.0
    months = days / 30.4375
    return {"days": round(days, 1), "months": round(months, 2)}


def run_ap(ap: str) -> dict:
    log(f"=== AP {ap} ===")
    bdir = build_dir(ap)
    pf.camp.BUILD = bdir
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint = RAW_DIR / f"{ap}_{RUN_ID}_ppk.json"
    rx_log = ROOT / "experiments" / "final_1min_100_rx.log"

    build_firmware(ap)
    prod.stop_ppk_hold()
    pf.camp.ppk_power_off()
    time.sleep(2.0)
    ppk_proc = start_ppk_integrate(checkpoint)
    try:
        port = pf.camp.flash(erase=True)
        log(f"flash ok port={port}")
        rx = wait_run_done(rx_log, ppk_proc)
    finally:
        stop_proc(ppk_proc, grace_s=8.0)
        time.sleep(1)

    if not checkpoint.exists():
        raise RuntimeError(f"PPK checkpoint missing: {checkpoint}")
    ppk = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
    avg_mA = float(ppk.get("avg_current_mA", 0))
    energy_J = float(ppk.get("energy_J", 0))
    charge_mAh = float(ppk.get("charge_mAh", 0))
    elapsed = float(ppk.get("elapsed_s", 0))
    amortized = energy_J / K_HOT_ATTEMPTS if K_HOT_ATTEMPTS else 0.0
    life = cr2_life(avg_mA)

    row = {
        "ap": ap,
        "variant_id": VARIANT_ID,
        "full_success": rx["full_success"],
        "hot_attempts": K_HOT_ATTEMPTS,
        "rx_unique": rx["rx_unique"],
        "rx_dup": rx["rx_dup"],
        "loss_pct": rx["loss_pct"],
        "elapsed_s": elapsed,
        "total_energy_J": energy_J,
        "total_charge_mAh": charge_mAh,
        "avg_current_mA": avg_mA,
        "amortized_J_per_message": round(amortized, 9),
        "cr2_life_days": life["days"],
        "cr2_life_months": life["months"],
        "ppk_checkpoint": str(checkpoint),
        "run_id": RUN_ID,
    }
    log(
        f"{ap} energy_J={energy_J:.6f} charge_mAh={charge_mAh:.6f} "
        f"avg_mA={avg_mA:.6f} rx={rx['rx_unique']}/{K_HOT_ATTEMPTS}"
    )
    return row


def write_report(rows: list[dict]) -> None:
    lines = [
        "# Prepared final 1-minute 100-HOT energy test",
        "",
        f"Run id: `{RUN_ID}`",
        "",
        "One FULL Æther prepare + 100 HOT sends at ~60 s cadence per AP.",
        "PPK integral spans FULL startup through HOT #100 final deep sleep.",
        "",
        "ENCODE_OVERLAP_IMPLEMENTED=yes (Wi-Fi association starts before EncodePacket).",
        "",
        "| AP | FULL | HOT attempts | RX unique | loss | elapsed | total energy J | "
        "total charge mAh | avg current mA | amortized J/message | CR2 life |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        full = "yes" if r["full_success"] else "no"
        loss = f"{r['loss_pct']:.1f}%"
        life = f"{r['cr2_life_days']:.0f} d / {r['cr2_life_months']:.1f} mo"
        lines.append(
            f"| {r['ap']} | {full} | {r['hot_attempts']} | {r['rx_unique']} | "
            f"{loss} | {r['elapsed_s']:.0f} s | {r['total_energy_J']:.6f} | "
            f"{r['total_charge_mAh']:.6f} | {r['avg_current_mA']:.6f} | "
            f"{r['amortized_J_per_message']:.9f} | {life} |"
        )
    lines += [
        "",
        "Amortized J/message = total run energy / 100 (includes 1/100 FULL cost and sleep).",
        "",
        "ENCODE_OVERLAP_PRODUCTION_READY: pending result analysis (defaults unchanged).",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": RUN_ID,
        "variant_id": VARIANT_ID,
        "hot_sleep_ms": K_HOT_SLEEP_MS,
        "hot_attempts": K_HOT_ATTEMPTS,
        "ppk_voltage_mv": PPK_VOLTAGE_MV,
        "encode_overlap_implemented": "yes",
        "encode_overlap_production_ready": "pending",
        "runs": rows,
    }
    RESULT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"report -> {REPORT_MD}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ap", choices=APS_ORDER, help="Run single AP only")
    args = ap.parse_args()
    aps = [args.ap] if args.ap else APS_ORDER

    rx_proc = None
    rows: list[dict] = []
    rx_log = ROOT / "experiments" / "final_1min_100_rx.log"
    try:
        prod.kill_probe_receiver()
        prod.kill_orphan_serial_tails()
        if rx_log.exists():
            rx_log.unlink()
        start_receiver_log(rx_log)
        for ap_name in aps:
            rows.append(run_ap(ap_name))
    finally:
        prod.kill_probe_receiver()
        prod.stop_ppk_hold()

    write_report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
