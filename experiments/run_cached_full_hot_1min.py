#!/usr/bin/env python3
"""Chirkov: warmup FULL (uncounted), then 1 cached FULL + 10 HOT @ 60 s.

Best config (P4, SKIP_VALIDATE, CPU 80, FULL teardown, PS NONE, DISC_PM ON,
encode-during-association, PRE=25 ms). PPK online integral with wake/sleep
segments. Sleep is kept in the combined window; per-send burst energy is
separated so FULL, HOT, and sleep/minute can be reported independently.
"""
from __future__ import annotations

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
HOT_ATTEMPTS = 10
PERIOD_MS = 60_000
PPK_VOLTAGE_MV = 3000
CR2_MAH = 800.0
SLEEP_UA_ASSUMED = 8.0
FLAG_FULL = 0x80

BUILD_ROOT = ROOT / "build-cached-full-hot" / RUN_ID
RAW_DIR = ROOT / "experiments" / "power_modes_raw" / "cached_full_hot_1min"
RESULTS = ROOT / "experiments" / "power_factor_results"
REPORT_MD = ROOT / "experiments" / "PREPARED_CACHED_FULL_VS_HOT_1MIN.md"
RESULT_JSON = RESULTS / "cached_full_hot_1min.json"
PROGRESS = ROOT / "experiments" / "cached_full_hot_1min_progress.log"
RX_LOG = ROOT / "experiments" / "cached_full_hot_1min_rx.log"

AETHER = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")
RX_BUILD = AETHER / "build-probe-receiver"
RX_EXE = RX_BUILD / "probe-receiver.exe"


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " CFH " + msg
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


def build_firmware() -> Path:
    bdir = BUILD_ROOT / "fw"
    if bdir.exists():
        shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True, exist_ok=True)
    pf.camp.BUILD = bdir
    pf.BUILD = bdir
    wifi = pf.camp.APS[AP]
    pf.camp.seed_usb_console_sdkconfig()
    pf.force_sdk_measured(VARIANT_ID)
    extra = {
        "AE_EXP_CACHED_FULL_HOT_1MIN": "1",
        "AE_EXP_ENCODE_DURING_ASSOCIATION": "1",
        "AE_EXP_ENCODE_OVERLAP_LEGACY_EARLY_SOCKET": "",
        "AE_EXP_ENCODE_OVERLAP_DIAG": "",
        "AE_EXP_PREPARED_POWER_FACTOR": "",
        "AE_EXP_PREPARED_FINAL_1MIN_100": "",
        "AE_EXP_FULL_1MIN_10": "",
        "AE_POWER_BENCH_ARM_MS": "0",
    }
    args = cmake_common_args(bdir, wifi)
    args.extend(
        [
            f"-DAE_POWER_BENCH_VARIANT={VARIANT_ID}",
            "-DAE_POWER_BENCH_ARM_MS=0",
            f"-DAETHER_POWER_BENCH_HOT_ATTEMPTS={HOT_ATTEMPTS}",
            f"-DAETHER_POWER_BENCH_HOT_SLEEP_MS={PERIOD_MS}",
            f"-DAETHER_PREPARED_HOT_SLEEP_MS={PERIOD_MS}",
            f"-DAETHER_PREPARED_NONCE_RESERVE={HOT_ATTEMPTS + 10}",
        ]
    )
    args.extend(pf.clear_power_exp_flags(extra))
    for k, v in extra.items():
        args.append(f"-D{k}={v}")
    log(f"cmake -> {bdir}")
    pf.camp.clean_ninja_logs()
    r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    if r.returncode != 0:
        (bdir / "cmake.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("cmake failed")
    pf.force_sdk_measured(VARIANT_ID)
    log("ninja")
    ninja_build(bdir)
    return bdir


def start_ppk(checkpoint: Path) -> subprocess.Popen:
    script = ROOT / "experiments" / "ppk2_integrate_run.py"
    pid_file = ROOT / "experiments" / "ppk2_integrate.pid"
    if pid_file.exists():
        try:
            pid_file.unlink()
        except OSError:
            pass
    proc = subprocess.Popen(
        [
            str(pf.camp.PPK_PY),
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


def parse_rx(text: str) -> dict:
    arm = "BENCH_ARM" in text
    full_seqs = []
    hot_seqs = []
    for m in re.finditer(
        r"BENCH_DATA .* seq=(\d+) flags=(\d+)", text
    ):
        seq = int(m.group(1))
        flags = int(m.group(2))
        if flags & FLAG_FULL:
            full_seqs.append(seq)
        else:
            hot_seqs.append(seq)
    return {
        "arm": arm,
        "full_unique": len(set(full_seqs)),
        "full_dup": len(full_seqs) - len(set(full_seqs)),
        "hot_unique": len(set(hot_seqs)),
        "hot_dup": len(hot_seqs) - len(set(hot_seqs)),
        "hot_max": max(hot_seqs) if hot_seqs else 0,
        "full_seqs": sorted(set(full_seqs)),
        "hot_seqs": sorted(set(hot_seqs)),
    }


def pair_bursts(segments: list[dict]) -> list[dict]:
    wakes = [s for s in segments if s.get("kind") == "wake"]
    sleeps = [s for s in segments if s.get("kind") == "sleep"]
    bursts = []
    for w in wakes:
        sl = next((s for s in sleeps if s["t_s"] > w["t_s"]), None)
        if sl is None:
            continue
        bursts.append(
            {
                "t_wake_s": w["t_s"],
                "t_sleep_s": sl["t_s"],
                "duration_s": sl["t_s"] - w["t_s"],
                "energy_J": sl["energy_J"] - w["energy_J"],
                "e_wake_J": w["energy_J"],
                "e_sleep_J": sl["energy_J"],
            }
        )
    return bursts


def wait_run(rx_log: Path, timeout_s: float, on_arm=None) -> dict:
    t0 = time.time()
    arm_at = None
    last_hot_at = None
    cadence_after_arm = (1 + HOT_ATTEMPTS) * (PERIOD_MS / 1000.0) + 180.0
    while time.time() - t0 < timeout_s:
        text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
        rx = parse_rx(text)
        if rx["arm"] and arm_at is None:
            arm_at = time.time()
            log("BENCH_ARM warmup seen (uncounted)")
            if on_arm is not None:
                on_arm()
        elif (not rx["arm"]) and rx["full_unique"] >= 1 and arm_at is None:
            arm_at = time.time()
            log("FULL RX without ARM; skip warmup burst by duration")
        if rx["hot_max"] >= HOT_ATTEMPTS:
            if last_hot_at is None:
                last_hot_at = time.time()
                log(
                    f"HOT seq{HOT_ATTEMPTS} unique={rx['hot_unique']} "
                    f"FULL_RX={rx['full_unique']}"
                )
            elif time.time() - last_hot_at >= 20:
                return rx
        if arm_at and (time.time() - arm_at) >= cadence_after_arm:
            log(
                f"cadence done FULL_RX={rx['full_unique']} "
                f"HOT_RX={rx['hot_unique']}/{HOT_ATTEMPTS}"
            )
            return rx
        time.sleep(2.0)
    text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
    return parse_rx(text)


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


def analyze(ppk: dict, rx: dict, arm_elapsed_s: float | None) -> dict:
    segs = ppk.get("segments") or []
    bursts_all = pair_bursts(segs)
    if arm_elapsed_s is not None:
        gate = arm_elapsed_s + 8.0
        measured = [b for b in bursts_all if b["t_wake_s"] >= gate]
    else:
        # BENCH_ARM can be lost even when the device armed. Skip flash+warmup
        # (first long burst) and take the next 11 wakes: 1 FULL + 10 HOT.
        measured = list(bursts_all)
        if measured and measured[0]["duration_s"] >= 20.0:
            measured = measured[1:]
    if len(measured) > 11:
        measured = measured[:11]
    full_b = measured[0] if measured else None
    hot_b = measured[1:] if len(measured) > 1 else []
    sleeps = []
    for i in range(len(measured) - 1):
        a = measured[i]
        b = measured[i + 1]
        dt = b["t_wake_s"] - a["t_sleep_s"]
        de = b["e_wake_J"] - a["e_sleep_J"]
        sleeps.append({"duration_s": dt, "energy_J": de})
    full_j = float(full_b["energy_J"]) if full_b else 0.0
    hot_js = [float(b["energy_J"]) for b in hot_b]
    hot_avg = (sum(hot_js) / len(hot_js)) if hot_js else 0.0
    if full_b and hot_b:
        e0 = full_b["e_wake_J"]
        e1 = hot_b[-1]["e_sleep_J"]
        t0 = full_b["t_wake_s"]
        t1 = hot_b[-1]["t_sleep_s"]
        total = e1 - e0
        elapsed = t1 - t0
    else:
        total = 0.0
        elapsed = 0.0
        e0 = 0.0
        e1 = 0.0
    sleep_j = sum(s["energy_J"] for s in sleeps)
    sleep_t = sum(s["duration_s"] for s in sleeps)
    sleep_per_min = (sleep_j / sleep_t * 60.0) if sleep_t > 0 else 0.0
    sleep_ua = 0.0
    if sleep_t > 0 and sleep_j > 0:
        # E = V * I * t  => I = E / (V * t)
        sleep_ua = sleep_j / ((PPK_VOLTAGE_MV / 1000.0) * sleep_t) * 1e6
    assumed_sleep_min = (
        (PPK_VOLTAGE_MV / 1000.0) * (SLEEP_UA_ASSUMED * 1e-6) * 60.0
    )
    remainder = total - full_j
    hot_plus_sleep_per = remainder / HOT_ATTEMPTS if HOT_ATTEMPTS else 0.0
    avg_mA_window = 0.0
    if elapsed > 0:
        avg_mA_window = (total / elapsed) / (PPK_VOLTAGE_MV / 1000.0) * 1000.0
    hot_avg_mA_if_1min = hot_avg / 60.0 / (PPK_VOLTAGE_MV / 1000.0) * 1000.0
    # Duty: 1 HOT + 1 min sleep amortized (use measured HOT + measured sleep/min)
    per_min_hot_scenario = hot_avg + sleep_per_min
    per_min_mA = per_min_hot_scenario / 60.0 / (PPK_VOLTAGE_MV / 1000.0) * 1000.0
    return {
        "arm_elapsed_s": arm_elapsed_s,
        "segment_count": len(segs),
        "bursts_all": len(bursts_all),
        "measured_bursts": len(measured),
        "full_send_J": full_j,
        "full_duration_s": float(full_b["duration_s"]) if full_b else 0.0,
        "hot_sends_J": hot_js,
        "hot_avg_J": hot_avg,
        "hot_min_J": min(hot_js) if hot_js else 0.0,
        "hot_max_J": max(hot_js) if hot_js else 0.0,
        "sleep_gaps": sleeps,
        "sleep_total_J": sleep_j,
        "sleep_total_s": sleep_t,
        "sleep_J_per_min": sleep_per_min,
        "sleep_uA_measured": sleep_ua,
        "assumed_sleep_J_per_min": assumed_sleep_min,
        "window_start_energy_J": e0,
        "window_end_energy_J": e1,
        "window_total_J": total,
        "window_elapsed_s": elapsed,
        "window_minus_full_J": remainder,
        "hot_plus_sleep_avg_J": hot_plus_sleep_per,
        "window_avg_mA": avg_mA_window,
        "hot_active_avg_mA_if_spread_60s": hot_avg_mA_if_1min,
        "hot_plus_sleep_per_min_J": per_min_hot_scenario,
        "hot_plus_sleep_avg_mA": per_min_mA,
        "cr2_hot_plus_sleep": cr2_life(per_min_mA),
        "measured_burst_table": measured,
        "full_rx": rx["full_unique"],
        "hot_rx": rx["hot_unique"],
        "hot_dup": rx["hot_dup"],
        "full_dup": rx["full_dup"],
        "full_loss_pct": round(100.0 * max(0, 1 - rx["full_unique"]) / 1, 2),
        "hot_loss_pct": round(
            100.0 * max(0, HOT_ATTEMPTS - rx["hot_unique"]) / HOT_ATTEMPTS, 2
        ),
    }


def write_report(row: dict, ppk: dict) -> None:
    lines = [
        "# Cached FULL vs HOT — chirkov 1-minute",
        "",
        f"Run id: `{RUN_ID}`",
        f"Starting SHA: `{STARTING_SHA}`",
        "",
        "Warmup FULL (IP/channel/ARP + prepared block) is **not** in the energy window.",
        "Window: start of 2nd (cached) FULL send → end of HOT #10.",
        "Cadence: ≈60 s start-to-start. Best config P4 / CPU80 / SKIP_VALIDATE /",
        "FULL teardown / WIFI_PS=NONE / DISC_PM=ON / encode-during-association / PRE=25 ms.",
        "",
        "| send | attempts | RX | loss | energy J | duration s |",
        "|---|---:|---:|---:|---:|---:|",
        f"| FULL (cached) | 1 | {row['full_rx']} | {row['full_loss_pct']}% | "
        f"{row['full_send_J']:.6f} | {row['full_duration_s']:.3f} |",
        f"| HOT | {HOT_ATTEMPTS} | {row['hot_rx']} | {row['hot_loss_pct']}% | "
        f"{row['hot_avg_J']:.6f} avg "
        f"({row['hot_min_J']:.6f}–{row['hot_max_J']:.6f}) | — |",
        "",
        f"WINDOW_TOTAL_J={row['window_total_J']:.9f}",
        f"FULL_SEND_J={row['full_send_J']:.9f}",
        f"WINDOW_MINUS_FULL_J={row['window_minus_full_J']:.9f}",
        f"HOT_AVG_J={row['hot_avg_J']:.9f}",
        f"SLEEP_J_PER_MIN={row['sleep_J_per_min']:.9f}",
        f"SLEEP_uA_MEASURED={row['sleep_uA_measured']:.3f}",
        f"ASSUMED_SLEEP_8uA_J_PER_MIN={row['assumed_sleep_J_per_min']:.9f}",
        "",
        "Decomposition check: WINDOW_MINUS_FULL ≈ 10×HOT_AVG + sleep in the gaps.",
        f"HOT_PLUS_SLEEP_PER_MIN_J={row['hot_plus_sleep_per_min_J']:.9f}",
        f"HOT_PLUS_SLEEP_AVG_mA={row['hot_plus_sleep_avg_mA']:.6f}",
        f"CR2_HOT_1MSG_PER_MIN={row['cr2_hot_plus_sleep']['days']} d / "
        f"{row['cr2_hot_plus_sleep']['months']} mo",
        "",
        "Per-send bursts (after skipping flash+warmup):",
        f"  FULL duration={row['full_duration_s']:.3f}s energy={row['full_send_J']:.6f} J",
        "Per HOT burst (J): "
        + ", ".join(f"{x:.6f}" for x in row["hot_sends_J"]),
        "",
        f"measured_bursts={row['measured_bursts']} (want 11 = 1 FULL + 10 HOT)",
        f"PPK elapsed_s={ppk.get('elapsed_s')} energy_J={ppk.get('energy_J')}",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RESULTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": RUN_ID,
        "starting_sha": STARTING_SHA,
        "ap": AP,
        "period_ms": PERIOD_MS,
        "config": {
            "profile": "P4",
            "cpu_mhz": 80,
            "skip_validate": True,
            "full_teardown": True,
            "wifi_ps": "NONE",
            "disc_pm": True,
            "encode_during_association": True,
            "pre_ms": 25,
            "post_ms": 0,
            "bssid_pin": False,
            "static_ipv4": True,
            "static_arp": True,
            "cached_channel": True,
        },
        "result": row,
        "ppk": {
            "elapsed_s": ppk.get("elapsed_s"),
            "energy_J": ppk.get("energy_J"),
            "charge_mAh": ppk.get("charge_mAh"),
            "avg_current_mA": ppk.get("avg_current_mA"),
        },
    }
    RESULT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log(f"report -> {REPORT_MD}")


def main() -> int:
    log(f"starting_sha={STARTING_SHA}")
    bdir = build_firmware()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ckpt = RAW_DIR / f"run_{RUN_ID}_ppk.json"
    prod.kill_probe_receiver()
    time.sleep(0.5)
    if RX_LOG.exists():
        try:
            RX_LOG.unlink()
        except OSError:
            RX_LOG.write_text("", encoding="utf-8")
    if not RX_EXE.is_file():
        subprocess.run(["cmake", "--build", str(RX_BUILD), "--parallel"], check=False)
    prod.start_receiver(RX_LOG)

    arm_elapsed = {"s": None}
    ppk_t0 = {"t": None}

    def on_arm() -> None:
        if ppk_t0["t"] is not None:
            arm_elapsed["s"] = time.time() - ppk_t0["t"]
            log(f"arm_elapsed_s={arm_elapsed['s']:.3f}")

    prod.stop_ppk_hold()
    pf.camp.ppk_power_off()
    time.sleep(1.5)
    ppk = None
    try:
        ppk = start_ppk(ckpt)
        ppk_t0["t"] = time.time()
        pf.camp.BUILD = bdir
        port = pf.camp.flash(erase=True)
        log(f"flash ok {port}")
        rx = wait_run(RX_LOG, timeout_s=1500, on_arm=on_arm)
    finally:
        stop_proc(ppk)
        time.sleep(1)
        prod.kill_probe_receiver()
        prod.stop_ppk_hold()

    ppk_data = read_ppk(ckpt)
    row = analyze(ppk_data, rx, arm_elapsed["s"])
    log(
        f"FULL_J={row['full_send_J']:.6f} HOT_AVG_J={row['hot_avg_J']:.6f} "
        f"SLEEP_J/min={row['sleep_J_per_min']:.6f} "
        f"WINDOW_J={row['window_total_J']:.6f} "
        f"RX FULL={row['full_rx']}/1 HOT={row['hot_rx']}/{HOT_ATTEMPTS}"
    )
    write_report(row, ppk_data)
    ok = (
        rx["arm"]
        and row["measured_bursts"] >= 11
        and True
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
