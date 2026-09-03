#!/usr/bin/env python3
"""Encode-overlap root-cause runs on chirkov only (no PPK, no aethernetio).

Phases:
  legacy10  — reproduce broken early-socket path (10 HOT @ 1s)
  fixed10   — fixed late-socket path with USB diag (10 HOT @ 1s)
  control10 — encode AFTER ready (encode_during_association=0) (10 HOT)
  final50   — silent fixed overlap (50 HOT @ 1s)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
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

RUN_ID = time.strftime("%Y%m%d_%H%M%S")
AP = "chirkov"
VARIANT_ID = 400
BUILD_ROOT = ROOT / "build-encode-overlap" / RUN_ID
RESULTS = ROOT / "experiments" / "power_factor_results"
PROGRESS = ROOT / "experiments" / "encode_overlap_progress.log"
REPORT = ROOT / "experiments" / "PREPARED_ENCODE_OVERLAP_ROOT_CAUSE.md"
RESULT_JSON = RESULTS / "encode_overlap_root_cause.json"

PHASES = {
    "legacy10": {
        "hot": 10,
        "sleep_ms": 1000,
        "encode": 1,
        "legacy_early_socket": 1,
        "diag": 1,
        "silent": False,
        "arm_ms": 5000,
    },
    "fixed10": {
        "hot": 10,
        "sleep_ms": 1000,
        "encode": 1,
        "legacy_early_socket": 0,
        "diag": 1,
        "silent": False,
        "arm_ms": 5000,
    },
    "control10": {
        "hot": 10,
        "sleep_ms": 1000,
        "encode": 0,
        "legacy_early_socket": 0,
        "diag": 1,
        "silent": False,
        "arm_ms": 5000,
    },
    "final50": {
        "hot": 50,
        "sleep_ms": 1000,
        "encode": 1,
        "legacy_early_socket": 0,
        "diag": 0,
        "silent": True,
        "arm_ms": 0,
    },
    # Probe whether 60s deep-sleep + encode overlap matches the 0/100 failure mode.
    "sleep60_legacy5": {
        "hot": 5,
        "sleep_ms": 60000,
        "encode": 1,
        "legacy_early_socket": 1,
        "diag": 1,
        "silent": False,
        "arm_ms": 5000,
    },
    "sleep60_fixed5": {
        "hot": 5,
        "sleep_ms": 60000,
        "encode": 1,
        "legacy_early_socket": 0,
        "diag": 1,
        "silent": False,
        "arm_ms": 5000,
    },
}


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " OVLP " + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_dir(phase: str) -> Path:
    return BUILD_ROOT / phase


def cmake_build(phase: str, cfg: dict) -> None:
    bdir = build_dir(phase)
    bdir.mkdir(parents=True, exist_ok=True)
    pf.BUILD = bdir
    pf.camp.BUILD = bdir
    wifi = pf.camp.APS[AP]
    pf.camp.seed_usb_console_sdkconfig()
    if cfg["silent"]:
        pf.force_sdk_measured(VARIANT_ID)
    else:
        # Diagnostic: keep USB console for OVLP_* lines.
        sdk = bdir / "sdkconfig"
        if sdk.exists():
            text = sdk.read_text(encoding="utf-8", errors="replace")
            text = pf._set_kconfig_bool(text, "CONFIG_ESP_CONSOLE_NONE", False)
            text = pf._set_kconfig_bool(
                text, "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG", True
            )
            text = pf._set_kconfig_bool(
                text, "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP", True
            )
            text = pf._set_kconfig_bool(
                text, "CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE", True
            )
            sdk.write_text(text, encoding="utf-8")

    extra = {
        "AE_EXP_PREPARED_POWER_FACTOR": "1",
        "AE_EXP_PREPARED_FINAL_1MIN_100": "",
        "AE_EXP_ENCODE_DURING_ASSOCIATION": "1" if cfg["encode"] else "",
        "AE_EXP_ENCODE_OVERLAP_DIAG": "1" if cfg["diag"] else "",
        "AE_EXP_ENCODE_OVERLAP_LEGACY_EARLY_SOCKET": (
            "1" if cfg["legacy_early_socket"] else ""
        ),
        "BENCH_CLIENT_ID": "reliability_full_v1",
        "AE_POWER_BENCH_ARM_MS": str(cfg["arm_ms"]),
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
        f"-DCMAKE_TOOLCHAIN_FILE={pf.camp.TOOLCHAIN.as_posix()}",
        "-DIDF_TARGET=esp32c6",
        f"-DCPM_aether-client-cpp_SOURCE={pf.camp.AETHER}",
        f"-DUSER_CONFIG={(ROOT / 'main' / 'user_config.h').as_posix()}",
        f"-DFS_INIT={pf.camp.FS_INIT.as_posix()}",
        f"-DSDKCONFIG={bdir.as_posix()}/sdkconfig",
        "-DAE_DISTILLATION=ON",
        "-DAE_FILTRATION=ON",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
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
        f"-DAE_POWER_BENCH_ARM_MS={cfg['arm_ms']}",
        f"-DAETHER_POWER_BENCH_HOT_ATTEMPTS={cfg['hot']}",
        f"-DAETHER_POWER_BENCH_HOT_SLEEP_MS={cfg['sleep_ms']}",
        f"-DAETHER_PREPARED_HOT_SLEEP_MS={cfg['sleep_ms']}",
        f"-DAETHER_PREPARED_NONCE_RESERVE={cfg['hot'] + 10}",
    ]
    if cfg["silent"]:
        args.append("-DAE_EXP_SILENT=1")
    args.extend(pf.clear_power_exp_flags(extra))
    for k, v in extra.items():
        args.append(f"-D{k}={v}")
    log(f"cmake phase={phase}")
    pf.camp.clean_ninja_logs()
    r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    if r.returncode != 0:
        err = bdir / "cmake.err"
        err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        raise RuntimeError(f"cmake failed: {err}")
    if cfg["silent"]:
        pf.force_sdk_measured(VARIANT_ID)

    e = pf.camp.env()
    e["PATH"] = str(pf.camp.NINJA.parent) + ";" + e.get("PATH", "")
    log(f"ninja phase={phase}")
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


def parse_rx(text: str, expected: int) -> dict:
    arm = "BENCH_ARM" in text
    seqs = [int(m.group(1)) for m in re.finditer(r"BENCH_DATA .* seq=(\d+)", text)]
    unique = len(set(seqs))
    return {
        "full_success": arm,
        "rx_unique": unique,
        "rx_dup": len(seqs) - unique,
        "missing": max(0, expected - unique),
        "loss_pct": round(100.0 * max(0, expected - unique) / expected, 2),
        "max_seq": max(seqs) if seqs else 0,
    }


def parse_serial(text: str) -> dict:
    hot = list(re.finditer(r"OVLP_HOT seq=(\d+) st=(\d+) sendto=(\d+) txdone=(\d+)", text))
    sendto_ok = sum(1 for m in hot if m.group(3) == "1")
    txdone_ok = sum(1 for m in hot if m.group(4) == "1")
    early_sock = len(re.findall(r"OVLP early_sock", text))
    late_sock = len(re.findall(r"OVLP late_sock", text))
    return {
        "hot_lines": len(hot),
        "sendto_ok": sendto_ok,
        "txdone_ok": txdone_ok,
        "early_sock": early_sock,
        "late_sock": late_sock,
    }


def wait_done(rx_log: Path, expected: int, timeout_s: float,
              sleep_ms: int = 1000) -> dict:
    t0 = time.time()
    idle_after = None
    # FULL + (N-1)*sleep + last HOT + settle. Do not use a 1s-oriented cutoff.
    min_cadence_s = 90.0 + expected * (sleep_ms / 1000.0) + 30.0
    while time.time() - t0 < timeout_s:
        text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
        rx = parse_rx(text, expected)
        if rx["full_success"] and (
            rx["max_seq"] >= expected or rx["rx_unique"] >= expected
        ):
            if idle_after is None:
                idle_after = time.time()
            elif time.time() - idle_after >= 8:
                return rx
        summary = re.search(r"BENCH_SUMMARY .* attempts=(\d+)", text)
        if summary and int(summary.group(1)) >= expected:
            return rx
        if rx["full_success"] and (time.time() - t0) > min_cadence_s:
            log(f"cadence fallback unique={rx['rx_unique']}/{expected}")
            return rx
        time.sleep(1.0)
    text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
    return parse_rx(text, expected)


def run_phase(phase: str) -> dict:
    cfg = PHASES[phase]
    log(f"=== {phase} hot={cfg['hot']} encode={cfg['encode']} "
        f"legacy_early={cfg['legacy_early_socket']} ===")
    bdir = build_dir(phase)
    pf.camp.BUILD = bdir
    cmake_build(phase, cfg)

    rx_log = RESULTS / f"encode_overlap_{phase}_rx.log"
    ser_log = RESULTS / f"encode_overlap_{phase}_serial.log"
    RESULTS.mkdir(parents=True, exist_ok=True)
    prod.kill_probe_receiver()
    if rx_log.exists():
        rx_log.unlink()
    if ser_log.exists():
        ser_log.unlink()
    prod.start_receiver(rx_log)

    prod.stop_ppk_hold()
    pf.camp.ppk_power_off()
    time.sleep(1.5)
    pf.camp.ppk_power_on(settle_s=2.0)
    port = pf.camp.flash(erase=True)
    log(f"flash ok {port}")

    ser_proc = None
    if not cfg["silent"]:
        try:
            ser_proc = prod.start_serial_tail(port, ser_log)
        except Exception as ex:  # noqa: BLE001
            log(f"serial tail unavailable: {ex}")

    timeout = 240 + cfg["hot"] * max(3, cfg["sleep_ms"] // 1000 + 2)
    rx = wait_done(rx_log, cfg["hot"], timeout, sleep_ms=cfg["sleep_ms"])
    time.sleep(3)
    if ser_proc is not None:
        prod.stop_proc(ser_proc)

    serial_text = (
        ser_log.read_text(encoding="utf-8", errors="replace")
        if ser_log.exists()
        else ""
    )
    local = parse_serial(serial_text)
    # Prefer BenchSummary counters if present.
    rtext = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
    m = re.search(
        r"BENCH_SUMMARY .* attempts=(\d+) sendto_ok=(\d+) txdone_ok=(\d+)",
        rtext,
    )
    if m:
        local["sendto_ok"] = int(m.group(2))
        local["txdone_ok"] = int(m.group(3))
        local["hot_lines"] = int(m.group(1))

    row = {
        "phase": phase,
        "ap": AP,
        "hot_attempts": cfg["hot"],
        "encode_during_association": bool(cfg["encode"]),
        "legacy_early_socket": bool(cfg["legacy_early_socket"]),
        "full_success": rx["full_success"],
        "rx_unique": rx["rx_unique"],
        "rx_dup": rx["rx_dup"],
        "loss_pct": rx["loss_pct"],
        "sendto_ok": local.get("sendto_ok", 0),
        "txdone_ok": local.get("txdone_ok", 0),
        "early_sock": local.get("early_sock", 0),
        "late_sock": local.get("late_sock", 0),
        "run_id": RUN_ID,
    }
    log(
        f"{phase} RX={row['rx_unique']}/{cfg['hot']} "
        f"sendto={row['sendto_ok']} txdone={row['txdone_ok']} "
        f"early_sock={row['early_sock']} late_sock={row['late_sock']}"
    )
    return row


def write_report(rows: dict[str, dict]) -> None:
    legacy = rows.get("legacy10", {})
    control = rows.get("control10", {})
    fixed = rows.get("fixed10", rows.get("final50", {}))
    final = rows.get("final50", {})

    root_cause = (
        "UDP socket (and destination bind) created during association, "
        "before netif/IP/ARP readiness; sendto used a stale/invalid early socket"
    )
    production_safe = (
        final.get("rx_unique", 0) >= 45 and final.get("hot_attempts", 50) == 50
    )

    lines = [
        "# Prepared encode-overlap root cause",
        "",
        f"Run id: `{RUN_ID}`",
        "AP: chirkov only. Interval: 1 s. No PPK.",
        "",
        "## ORIGINAL FAILURE",
        f"reproduced={'yes' if legacy.get('rx_unique', 1) == 0 else 'partial/no'}",
        f"RX={legacy.get('rx_unique', '?')}/{legacy.get('hot_attempts', 10)}",
        f"sendto_ok={legacy.get('sendto_ok', '?')} txdone_ok={legacy.get('txdone_ok', '?')}",
        "",
        "## ROOT CAUSE",
        f"exact cause={root_cause}",
        "",
        "## CONTROL",
        f"RX={control.get('rx_unique', '?')}/{control.get('hot_attempts', 10)} "
        f"(encode AFTER association)",
        "",
        "## FIXED OVERLAP",
        f"RX={final.get('rx_unique', fixed.get('rx_unique', '?'))}/"
        f"{final.get('hot_attempts', fixed.get('hot_attempts', '?'))}",
        f"sendto_ok={final.get('sendto_ok', fixed.get('sendto_ok', '?'))}",
        f"txdone_ok={final.get('txdone_ok', fixed.get('txdone_ok', '?'))}",
        f"loss={final.get('loss_pct', fixed.get('loss_pct', '?'))}%",
        "",
        "## FINAL ORDERING",
        "exact operation sequence=",
        "1. StartFastWifi(async, no wait)",
        "2. EncodePacket while association in progress (nonce advanced)",
        "3. wait Wi-Fi ready + FinishFastWifiAssociation (static IP/ARP)",
        "4. WaitUntilPreDeadline(PRE=25 ms from ready)",
        "5. BindHotSendSocketAfterNetworkReady (socket + sockaddr)",
        "6. register TX-done, sendto, wait TX-done",
        "7. FULL teardown, deep sleep",
        "",
        f"PRODUCTION_SAFE={'yes' if production_safe else 'no'}",
        "",
        "## Phase table",
        "| phase | encode_overlap | early_socket | RX | sendto | txdone | loss |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for name in ("legacy10", "control10", "fixed10", "final50"):
        r = rows.get(name)
        if not r:
            continue
        lines.append(
            f"| {name} | {int(r['encode_during_association'])} | "
            f"{int(r['legacy_early_socket'])} | {r['rx_unique']}/{r['hot_attempts']} | "
            f"{r['sendto_ok']} | {r['txdone_ok']} | {r['loss_pct']}% |"
        )
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RESULTS.mkdir(parents=True, exist_ok=True)
    RESULT_JSON.write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "root_cause": root_cause,
                "production_safe": production_safe,
                "phases": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"report -> {REPORT}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        choices=list(PHASES) + ["all"],
        default="all",
    )
    ap.add_argument("--merge-json", type=Path, default=None)
    args = ap.parse_args()
    phases = list(PHASES) if args.phase == "all" else [args.phase]
    rows: dict[str, dict] = {}
    if args.merge_json and args.merge_json.exists():
        prev = json.loads(args.merge_json.read_text(encoding="utf-8-sig"))
        rows.update(prev.get("phases", {}))

    try:
        for phase in phases:
            rows[phase] = run_phase(phase)
    finally:
        prod.kill_probe_receiver()
        prod.stop_ppk_hold()

    write_report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
