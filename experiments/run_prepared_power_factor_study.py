#!/usr/bin/env python3
"""Autonomous ESP32-C6 prepared power-factor hardware campaign.

One variant id and access point per run. The firmware is silent during HOT100;
progress and the PPK arm point come from the TCP probe_receiver log only.

Per (variant, ap):
  1. cmake with AE_EXP_PREPARED_POWER_FACTOR and AE_POWER_BENCH_VARIANT
  2. sdkconfig patches for measured runs (external RTC, silent console/log)
  3. build, start probe_receiver, PPK power-off (clear RTC), start ppk2_hold
  4. erase-flash
  5. wait for BENCH_ARM in the receiver log (fail fast on stale HOT/NO_ARM)
  6. release PPK hold and log current for the HOT100 deep-sleep run
  7. wait for HOT100 completion (receiver)
  8. write raw CSV and a run summary under experiments/power_factor_results/

Checkpoint: experiments/power_factor_checkpoint.json
Raw CSV: experiments/power_modes_raw/<variant>_<ap>.csv
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build-esp32c6-pf-fresh"
RAW_DIR = ROOT / "experiments" / "power_modes_raw"
RESULTS = ROOT / "experiments" / "power_factor_results"
CHECKPOINT = ROOT / "experiments" / "power_factor_checkpoint.json"
PROGRESS = ROOT / "experiments" / "power_factor_progress.log"
CONFIGURED_MARK = ROOT / "experiments" / "power_factor_configured.txt"

AETHER = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")
RX_BUILD = AETHER / "build-probe-receiver"
RX_EXE = RX_BUILD / "probe-receiver.exe"
RX_CONFIG = AETHER / "examples" / "probe_receiver" / "user_config.h"

# From power_factor_config.h
K_HOT_ATTEMPTS = 100
K_HOT_SLEEP_MS = 2000
K_MIN_RX_UNIQUE = 90

# Phase A/B/C on chirkov, then IO subset on aethernetio.
CHIRKOV_VARIANTS = (
    [0, 1]
    + list(range(10, 23))
    + [100, 101, 110, 111, 112, 120, 121, 130, 131, 140, 150]
)
AETHERNETIO_VARIANTS = list(range(200, 209))

RUN_TIMEOUT_S = 4 * 60 * 60
ARM_TIMEOUT_S = 180
HOT_IDLE_DONE_S = 25
POLL_S = 1.0
SLOW_POLL_EVERY = 15
MAX_PPK_ATTEMPTS = 3

spec = importlib.util.spec_from_file_location(
    "camp", ROOT / "experiments" / "run_adaptive_wifi_probe_campaign.py"
)
camp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(camp)

prod_spec = importlib.util.spec_from_file_location(
    "prod", ROOT / "experiments" / "run_product_adaptive_probe.py"
)
prod = importlib.util.module_from_spec(prod_spec)
assert prod_spec.loader is not None
prod_spec.loader.exec_module(prod)

camp.BUILD = BUILD


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " PF " + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def variant_name(variant_id: int) -> str:
    names = {
        0: "A0_CLEAN",
        1: "A0_REPEAT",
        10: "B1_SKIP_VALIDATE_DEEP_SLEEP",
        11: "B2_DISC_PM_OFF",
        12: "B3_WIFI_PS_MIN",
        13: "B4_WIFI_PS_MAX_LI1",
        14: "B5_WIFI_PS_MAX_LI3",
        15: "B6_CPU120",
        16: "B7_CPU80",
        17: "B8_CPU40",
        18: "B9_PMF_OFF",
        19: "B10_ENCODE_DURING_ASSOCIATION",
        20: "B11_WIFI_STOP_ONLY",
        21: "B12_DIRECT_DEEP_SLEEP",
        22: "B13_PHY_PARTIAL_EVERY_WAKE",
        100: "C1_CPU_MIN",
        101: "C2_CPU_MAX_LI1",
        110: "C3_ALS_NONE",
        111: "C4_ALS_MIN",
        112: "C5_ALS_MAX_LI1",
        120: "C6_PRE_MIN_TO_NONE",
        121: "C7_PRE_MAX_TO_NONE",
        130: "C8_ENCODE_CPU80",
        131: "C9_PMF_OFF_CPU80",
        140: "C10_TEARDOWN_MATRIX",
        150: "C11_PHY_FINAL",
        200: "IO_A0",
        201: "IO_DISC_PM_OFF",
        202: "IO_BEST_CPU",
        203: "IO_BEST_PS",
        204: "IO_BEST_DFS",
        205: "IO_BEST_OVERALL",
        206: "IO_TEARDOWN",
        207: "IO_PMF_OFF",
        208: "IO_PHY",
        300: "CFM_IO_TEARDOWN_SKIP_VALIDATE",
        301: "CFM_IO_TEARDOWN_WIFI_PS_MIN",
        302: "CFM_IO_TEARDOWN_CPU80",
        303: "CFM_IO_TEARDOWN_DISC_PM_OFF",
        310: "CFM_IO_TEARDOWN_SKIP_PS_MIN",
        311: "CFM_IO_TEARDOWN_SKIP_CPU80",
        312: "CFM_IO_TEARDOWN_SKIP_PS_MIN_CPU80",
        313: "CFM_IO_TEARDOWN_ALL_CONFIRMED",
        314: "CFM_FULL_TEARDOWN_ALL_CONFIRMED",
    }
    return names.get(variant_id, f"V{variant_id}")


def variants_for_ap(ap: str) -> list[int]:
    if ap == "chirkov":
        return list(CHIRKOV_VARIANTS)
    if ap == "aethernetio":
        return list(AETHERNETIO_VARIANTS)
    raise ValueError(ap)


def load_checkpoint() -> dict:
    if not CHECKPOINT.exists():
        return {"task_index": 0, "results": {}}
    return json.loads(CHECKPOINT.read_text(encoding="utf-8-sig"))


def save_checkpoint(data: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(data, indent=2), encoding="utf-8")


def task_key(ap: str, variant_id: int) -> str:
    return f"{variant_id}_{ap}"


def iter_tasks(aps: list[str]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for ap in aps:
        for vid in variants_for_ap(ap):
            out.append((ap, vid))
    return out


# Variants that must enable bootloader deep-sleep image skip-validate.
# Prior campaign only *appended* this for variant 10 and never cleared it, so
# later builds in the shared dir inherited SKIP_VALIDATE (contamination).
SKIP_VALIDATE_VARIANTS = frozenset(
    {
        10,
        300,
        310,
        311,
        312,
        313,
        314,
    }
)

# Variants that must disable CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE.
# Runtime PowerBenchOptions.disconnected_pm is not wired to an API; the real
# factor is this Kconfig bit.
DISC_PM_OFF_VARIANTS = frozenset({11, 201, 303, 313, 314})


def _set_kconfig_bool(text: str, symbol: str, enabled: bool) -> str:
    """Force a boolean Kconfig symbol on or off; drop conflicting lines."""
    on = f"{symbol}=y"
    off = f"# {symbol} is not set"
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == on or stripped == off or stripped.startswith(f"{symbol}="):
            continue
        lines.append(line)
    lines.append(on if enabled else off)
    return "\n".join(lines) + "\n"


def force_sdk_measured(variant_id: int) -> None:
    """Silent measured-run sdkconfig: external RTC, no console/log noise.

    Sticky Kconfig factors (SKIP_VALIDATE, DISCONNECTED_PM) are set *and*
    cleared every call so a shared or reused build dir cannot leak state.
    """
    sdk = BUILD / "sdkconfig"
    if not sdk.exists():
        camp.seed_usb_console_sdkconfig()
    text = read_text(sdk)
    reps = [
        ("CONFIG_RTC_CLK_SRC_INT_RC=y", "# CONFIG_RTC_CLK_SRC_INT_RC is not set"),
        ("# CONFIG_RTC_CLK_SRC_EXT_CRYS is not set", "CONFIG_RTC_CLK_SRC_EXT_CRYS=y"),
        ("CONFIG_RTC_CLK_CAL_CYCLES=0", "CONFIG_RTC_CLK_CAL_CYCLES=1024"),
        ("CONFIG_RTC_CLK_CAL_CYCLES=577", "CONFIG_RTC_CLK_CAL_CYCLES=1024"),
        ("# CONFIG_LOG_DEFAULT_LEVEL_NONE is not set", "CONFIG_LOG_DEFAULT_LEVEL_NONE=y"),
        ("CONFIG_LOG_DEFAULT_LEVEL_INFO=y", "# CONFIG_LOG_DEFAULT_LEVEL_INFO is not set"),
        ("CONFIG_LOG_DEFAULT_LEVEL=3", "CONFIG_LOG_DEFAULT_LEVEL=0"),
        ("CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y", "# CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG is not set"),
        ("# CONFIG_ESP_CONSOLE_NONE is not set", "CONFIG_ESP_CONSOLE_NONE=y"),
        ("CONFIG_ESP_CONSOLE_UART_DEFAULT=y", "# CONFIG_ESP_CONSOLE_UART_DEFAULT is not set"),
        ("CONFIG_ESP_BROWNOUT_DET=y", "# CONFIG_ESP_BROWNOUT_DET is not set"),
    ]
    for a, b in reps:
        text = text.replace(a, b)
    for token in (
        "CONFIG_RTC_CLK_SRC_EXT_CRYS=y",
        "CONFIG_RTC_CLK_CAL_CYCLES=1024",
        "CONFIG_LOG_DEFAULT_LEVEL_NONE=y",
        "CONFIG_ESP_CONSOLE_NONE=y",
        "# CONFIG_ESP_BROWNOUT_DET is not set",
    ):
        if token not in text:
            text += f"\n{token}\n"

    # Never use ALWAYS / power-on skip in this study.
    text = _set_kconfig_bool(text, "CONFIG_BOOTLOADER_SKIP_VALIDATE_ALWAYS", False)
    text = _set_kconfig_bool(text, "CONFIG_BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON", False)
    text = _set_kconfig_bool(
        text,
        "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP",
        variant_id in SKIP_VALIDATE_VARIANTS,
    )
    text = _set_kconfig_bool(
        text,
        "CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE",
        variant_id not in DISC_PM_OFF_VARIANTS,
    )
    sdk.write_text(text, encoding="utf-8")


def clear_power_exp_flags(extra: dict[str, str]) -> list[str]:
    flags = [
        "AE_EXP_PREPARED_WIFI_CACHE_5X20",
        "AE_EXP_PREPARED_KEEP_WIFI_UP_5X20",
        "AE_EXP_PREPARED_WIFI_FASTEST",
        "AE_EXP_PREPARED_DEEPSLEEP_5X50",
        "AE_EXP_PREPARED_FINAL_D1_5X50",
        "AE_EXP_PREPARED_AP_AETHERNETIO_3X10",
        "AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5",
        "AE_EXP_FULL_AETHER_AETHERNETIO",
        "AE_EXP_ADAPTIVE_WIFI_PROBE_A",
        "AE_EXP_ADAPTIVE_WIFI_PROBE_B",
        "AE_EXP_ADAPTIVE_WIFI_PROBE_C",
        "AE_EXP_PRODUCT_ADAPTIVE_PROBE",
        "AE_EXP_PREPARED_TX_DONE_DIAG",
        "AE_EXP_PREPARED_MAC_RETRY_DIAG",
        "AE_EXP_PREPARED_BOOT_WIFI_OPT",
        "AE_EXP_PREPARED_BOOT_WIFI_VAL100",
        "AE_EXP_PREPARED_WIFI_BISECT",
        "AE_EXP_PREPARED_MESSAGE_E2E",
        "AE_EXP_WIFI_LIFECYCLE",
        "AE_EXP_FULL_CYCLES",
        "AE_EXP_PREPARED_POWER_FACTOR",
    ]
    out = []
    for f in flags:
        out.append(f"-D{f}={extra.get(f, '')}")
    return out


def cmake_configure_power(ap: str, variant_id: int) -> None:
    wifi = camp.APS[ap]
    if BUILD.exists() and not (BUILD / "CMakeCache.txt").exists():
        import shutil

        shutil.rmtree(BUILD, ignore_errors=True)
    cache = BUILD / "CMakeCache.txt"
    if cache.exists():
        text = read_text(cache)
        if "IDF_TARGET:STRING=esp32c6" not in text:
            import shutil

            log("wiping non-ESP-IDF build dir")
            shutil.rmtree(BUILD, ignore_errors=True)
        elif "CMAKE_CXX_COMPILER:FILEPATH=" in text and "msys64" in text:
            import shutil

            log("wiping host-msys build dir")
            shutil.rmtree(BUILD, ignore_errors=True)
    camp.seed_usb_console_sdkconfig()
    force_sdk_measured(variant_id)
    extra = {
        "AE_EXP_PREPARED_POWER_FACTOR": "1",
        "BENCH_CLIENT_ID": "reliability_full_v1",
    }
    args = [
        str(camp.CMAKE),
        "-S",
        str(ROOT),
        "-B",
        str(BUILD),
        "-G",
        "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={camp.TOOLCHAIN.as_posix()}",
        "-DIDF_TARGET=esp32c6",
        f"-DCPM_aether-client-cpp_SOURCE={camp.AETHER}",
        f"-DUSER_CONFIG={camp.USER_CONFIG}",
        f"-DFS_INIT={camp.FS_INIT.as_posix()}",
        f"-DSDKCONFIG={BUILD.as_posix()}/sdkconfig",
        "-DAE_DISTILLATION=ON",
        "-DAE_FILTRATION=ON",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DAE_EXP_SILENT=1",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DWIFI_SSID={wifi['ssid']}",
        f"-DWIFI_PASSWORD={wifi['password']}",
        f"-DSERVICE_UID={camp.SERVICE_UID}",
        f"-DPython3_EXECUTABLE={camp.PY.as_posix()}",
        f"-DCMAKE_MAKE_PROGRAM={camp.NINJA.as_posix()}",
        f"-DCMAKE_C_COMPILER={(camp.RISCV_BIN / 'riscv32-esp-elf-gcc.exe').as_posix()}",
        f"-DCMAKE_CXX_COMPILER={(camp.RISCV_BIN / 'riscv32-esp-elf-g++.exe').as_posix()}",
        f"-DCMAKE_OBJCOPY={(camp.RISCV_BIN / 'riscv32-esp-elf-objcopy.exe').as_posix()}",
        f"-DAE_POWER_BENCH_VARIANT={variant_id}",
        f"-DAETHER_PREPARED_HOT_SLEEP_SECONDS={K_HOT_SLEEP_MS // 1000}",
        f"-DAETHER_PREPARED_HOT_SLEEP_MS={K_HOT_SLEEP_MS}",
        f"-DAETHER_PREPARED_NONCE_RESERVE={K_HOT_ATTEMPTS + 10}",
    ]
    args.extend(clear_power_exp_flags(extra))
    for k, v in extra.items():
        args.append(f"-D{k}={v}")
    log(f"cmake ap={ap} variant={variant_id} ({variant_name(variant_id)})")
    camp.clean_ninja_logs()
    r = subprocess.run(args, cwd=ROOT, env=camp.env(), capture_output=True, text=True)
    if r.returncode != 0:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / f"cmake_{variant_id}_{ap}.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError(f"cmake failed variant={variant_id} ap={ap}")
    force_sdk_measured(variant_id)


def build_firmware(ap: str, variant_id: int) -> None:
    want = f"{ap}:v{variant_id}:r{K_HOT_ATTEMPTS + 10}:reliability_full_v1"
    have = CONFIGURED_MARK.read_text(encoding="utf-8").strip() if CONFIGURED_MARK.exists() else ""
    bin_path = BUILD / "temperature_sensor.bin"
    src_paths = [
        ROOT / "main" / "prepared_power_factor_bench.cpp",
        ROOT / "main" / "prepared_send" / "prepared_send.cpp",
    ]
    bin_fresh = bin_path.exists() and all(
        p.exists() and bin_path.stat().st_mtime >= p.stat().st_mtime for p in src_paths
    )
    if have != want:
        cmake_configure_power(ap, variant_id)
        CONFIGURED_MARK.write_text(want, encoding="utf-8")
    elif bin_fresh:
        log(f"cmake up to date for {want}")
    else:
        log(f"cmake up to date for {want}")
        force_sdk_measured(variant_id)
    if bin_fresh and have == want:
        log("build skipped (firmware up to date)")
        return
    camp.ninja_build()


def start_ppk_hold() -> subprocess.Popen | None:
    ppk_py = camp.PPK_PY
    script = ROOT / "experiments" / "ppk2_hold_power.py"
    if not ppk_py.exists() or not script.exists():
        log("PPK hold unavailable")
        return None
    prod.stop_ppk_hold()
    log_path = ROOT / "experiments" / "ppk2_hold.log"
    with log_path.open("a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            [str(ppk_py), str(script), "--voltage-mv", str(camp.PPK_VOLTAGE_MV)],
            cwd=str(ROOT / "experiments"),
            stdout=lf,
            stderr=subprocess.STDOUT,
        )
    time.sleep(3)
    if proc.poll() is not None:
        log("PPK hold exited immediately")
        return None
    log("PPK hold active")
    return proc


def parse_rx_progress(text: str) -> dict:
    bench_arm = "BENCH_ARM" in text
    bench_seq = [
        int(m.group(1))
        for m in re.finditer(r"BENCH_DATA .* seq=(\d+)", text)
    ]
    # Legacy product-probe HOT path (not used by power-factor bench firmware).
    hot_seq = [
        int(m.group(1))
        for m in re.finditer(
            rf"HOT_DATA .* sleep={K_HOT_SLEEP_MS} .* seq=(\d+)", text
        )
    ]
    if not hot_seq:
        hot_seq = [int(m.group(1)) for m in re.finditer(r"HOT_DATA .* seq=(\d+)", text)]
    all_seq = bench_seq if bench_seq else hot_seq
    unique = len(set(all_seq))
    max_seq = max(all_seq) if all_seq else 0
    summaries = list(
        re.finditer(
            r"BENCH_SUMMARY .* hot_attempts=(\d+)", text
        )
    )
    hot_summaries = list(
        re.finditer(r"HOT_SUMMARY .* hot_sent=(\d+) hot_fail=(\d+)", text)
    )
    summary = None
    if summaries:
        m = summaries[-1]
        summary = {"hot_attempts": int(m.group(1)), "hot_sent": int(m.group(1))}
    elif hot_summaries:
        m = hot_summaries[-1]
        summary = {"hot_sent": int(m.group(1)), "hot_fail": int(m.group(2))}
    bench_done = False
    if summary is not None:
        sent = summary.get("hot_attempts", summary.get("hot_sent", 0))
        if sent >= K_HOT_ATTEMPTS:
            bench_done = True
    # Accept max seq as done: one lost packet must not stall PPK forever.
    if unique >= K_HOT_ATTEMPTS or max_seq >= K_HOT_ATTEMPTS:
        bench_done = True
    return {
        "bench_arm": bench_arm,
        "bench_unique": unique,
        "hot_unique": unique,
        "hot_max_seq": max_seq,
        "hot_data_lines": len(all_seq),
        "summary": summary,
        "bench_done": bench_done,
    }


def run_variant_once(ap: str, variant_id: int, *, attempt: int = 1) -> dict:
    key = task_key(ap, variant_id)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    raw_csv = RAW_DIR / f"{variant_id}_{ap}.csv"
    rx_log = RESULTS / f"{variant_id}_{ap}_rx.log"
    run_json = RESULTS / f"{variant_id}_{ap}.json"

    prod.kill_probe_receiver()
    prod.kill_orphan_serial_tails()
    if rx_log.exists():
        rx_log.unlink()

    log(f"==== {key} attempt={attempt} ====")
    build_firmware(ap, variant_id)

    prod.start_receiver(rx_log)
    # Drop rail before hold/flash so RTC_NOINIT from a prior HOT session is
    # cleared; otherwise the board can resume HOT without BENCH_ARM.
    camp.ppk_power_off()
    time.sleep(2.0)
    hold = start_ppk_hold()
    if hold is None:
        return {
            "ap": ap,
            "variant_id": variant_id,
            "variant_name": variant_name(variant_id),
            "status": "PPK_HOLD_FAILED",
            "attempt": attempt,
        }

    port = camp.flash(erase=True)
    ppk: subprocess.Popen | None = None
    ppk_failed = False
    t0 = time.time()
    status = "TIMEOUT"
    rx_progress: dict = {}

    try:
        armed = False
        ticks = 0
        last_progress_key: tuple | None = None
        last_progress_at = time.time()
        while time.time() - t0 < RUN_TIMEOUT_S:
            ticks += 1
            if ticks % SLOW_POLL_EVERY == 0 and not prod.probe_receiver_alive():
                log("receiver dead - restart")
                prod.start_receiver(rx_log, append=True)
            rtext = read_text(rx_log)
            rx_progress = parse_rx_progress(rtext)

            if not armed and rx_progress["bench_arm"]:
                log("BENCH_ARM from receiver; starting PPK capture")
                prod.stop_ppk_hold()
                hold = None
                ppk = prod.start_ppk_log(raw_csv)
                if ppk is None:
                    ppk_failed = True
                    break
                armed = True
                last_progress_at = time.time()

            if not armed:
                elapsed = time.time() - t0
                stale_hot = rx_progress.get("hot_max_seq", 0) >= 1 or (
                    rx_progress.get("hot_unique", 0) >= 1
                )
                if elapsed >= ARM_TIMEOUT_S or (
                    stale_hot and elapsed >= 60 and not rx_progress["bench_arm"]
                ):
                    log(
                        f"NO_ARM after {int(elapsed)}s "
                        f"hot_max_seq={rx_progress.get('hot_max_seq', 0)} "
                        f"(stale RTC or boot failure)"
                    )
                    status = "NO_ARM"
                    break

            progress_key = (
                rx_progress.get("hot_unique", 0),
                rx_progress.get("hot_max_seq", 0),
                rx_progress.get("hot_data_lines", 0),
            )
            if progress_key != last_progress_key:
                last_progress_key = progress_key
                last_progress_at = time.time()

            if armed and rx_progress["bench_done"]:
                status = "OK"
                time.sleep(5)
                break
            if armed and rx_progress.get("summary"):
                s = rx_progress["summary"]
                if s["hot_sent"] >= K_HOT_ATTEMPTS:
                    status = "OK"
                    time.sleep(5)
                    break
            if armed and rx_progress["hot_unique"] >= K_HOT_ATTEMPTS:
                status = "OK"
                time.sleep(5)
                break
            # Device finished HOT but last seq/summary never arrived (Wi-Fi loss).
            if (
                armed
                and rx_progress.get("hot_unique", 0) >= K_MIN_RX_UNIQUE
                and (time.time() - last_progress_at) >= HOT_IDLE_DONE_S
            ):
                log(
                    f"HOT idle-done unique={rx_progress.get('hot_unique')} "
                    f"max_seq={rx_progress.get('hot_max_seq')} "
                    f"idle>={HOT_IDLE_DONE_S}s"
                )
                status = "OK"
                time.sleep(2)
                break

            time.sleep(POLL_S)
    finally:
        prod.stop_proc(ppk)
        prod.stop_proc(hold)

    if ppk_failed:
        camp.ppk_power_off()
        time.sleep(3)
        return {
            "ap": ap,
            "variant_id": variant_id,
            "variant_name": variant_name(variant_id),
            "status": "PPK_CAPTURE_FAILED",
            "attempt": attempt,
        }

    csv_ok = raw_csv.exists() and raw_csv.stat().st_size > 100
    result = {
        "ap": ap,
        "variant_id": variant_id,
        "variant_name": variant_name(variant_id),
        "status": status,
        "attempt": attempt,
        "elapsed_s": int(time.time() - t0),
        "hot_sleep_ms": K_HOT_SLEEP_MS,
        "hot_attempts": K_HOT_ATTEMPTS,
        "ppk_csv": str(raw_csv),
        "ppk_capture_ok": csv_ok and armed,
        "rx": rx_progress,
    }
    run_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log(f"{key} status={status} hot_unique={rx_progress.get('hot_unique', 0)}")
    return result


def run_variant(ap: str, variant_id: int) -> dict:
    result: dict = {}
    retryable = {"PPK_CAPTURE_FAILED", "PPK_HOLD_FAILED", "NO_ARM", "TIMEOUT"}
    for attempt in range(1, MAX_PPK_ATTEMPTS + 1):
        result = run_variant_once(ap, variant_id, attempt=attempt)
        if result.get("status") not in retryable:
            return result
        log(f"retry {task_key(ap, variant_id)} after {result.get('status')}")
        camp.ppk_power_off()
        time.sleep(2.0)
    return result


def maybe_analyze(raw_csv: Path, ap: str, variant_id: int) -> None:
    analyze_py = ROOT / "experiments" / "analyze_power_factor_ppk.py"
    if not analyze_py.exists() or not raw_csv.exists():
        return
    subprocess.run(
        [
            sys.executable,
            str(analyze_py),
            "--csv",
            str(raw_csv),
            "--variant",
            str(variant_id),
            "--ap",
            ap,
        ],
        cwd=str(ROOT),
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ap", choices=("chirkov", "aethernetio", "all"), default="all")
    parser.add_argument("--variant", type=int, help="run a single variant id")
    parser.add_argument(
        "--from-checkpoint",
        action="store_true",
        help="resume from power_factor_checkpoint.json task_index",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned tasks without building or flashing",
    )
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if args.ap == "all":
        aps = ["chirkov", "aethernetio"]
    else:
        aps = [args.ap]

    tasks = iter_tasks(aps)
    if args.variant is not None:
        tasks = [(ap, args.variant) for ap in aps if args.variant in variants_for_ap(ap)]
        if not tasks:
            log(f"variant {args.variant} not in selected AP list")
            return 2

    if args.dry_run:
        start = 0
        if args.from_checkpoint:
            start = int(load_checkpoint().get("task_index", 0))
        for i, (ap, vid) in enumerate(tasks):
            mark = "skip" if args.from_checkpoint and i < start else "run"
            print(f"{mark}\t{vid}\t{variant_name(vid)}\t{ap}")
        return 0

    if not prod.acquire_run_lock():
        return 4
    try:
        prod.kill_orphan_serial_tails()
        prod.build_receiver()

        cp = load_checkpoint()
        results = cp.get("results", {})
        start = int(cp.get("task_index", 0)) if args.from_checkpoint else 0

        for i, (ap, vid) in enumerate(tasks):
            if i < start:
                continue
            key = task_key(ap, vid)
            results[key] = run_variant(ap, vid)
            save_checkpoint({"task_index": i + 1, "results": results})
            raw_csv = RAW_DIR / f"{vid}_{ap}.csv"
            maybe_analyze(raw_csv, ap, vid)

        prod.kill_probe_receiver()
        bad = [k for k, r in results.items() if r.get("status") != "OK"]
        log(f"campaign complete; failures={bad or 'none'}")
        return 0 if not bad else 3
    finally:
        prod.release_run_lock()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: {exc}")
        save_checkpoint(load_checkpoint() | {"fatal": str(exc)})
        raise
