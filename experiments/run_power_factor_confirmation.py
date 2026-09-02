#!/usr/bin/env python3
"""Power-factor CONFIRMATION campaign (isolated builds, not full 35-variant).

Confirms contaminated single-factor results with clean per-variant build dirs:
  build-power-confirm/<run>/<cfm_id>/<ap>/

Sequence:
  CFM00 A0 chirkov before
  CFM01-04 B1/B2/B3/B7
  CFM05 A0 chirkov after
  CFM06 IO_TEARDOWN(=DirectDeepSleep) chirkov
  CFM10-12 aethernetio A0 / IO_TEARDOWN / A0
  Then combinations of confirmed beneficial factors (after sections 6-9).

Does not repeat DIRECT_DEEP_SLEEP as a separate candidate beyond IO_TEARDOWN.
Does not touch the server.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import run_prepared_power_factor_study as pf  # noqa: E402
import batch_energy_report as energy  # noqa: E402

RUN_ID = time.strftime("%Y%m%d_%H%M%S")
CONFIRM_ROOT = ROOT / "experiments" / "power_factor_confirmation"
RAW_DIR = ROOT / "experiments" / "power_modes_raw" / "confirmation"
CHECKPOINT = CONFIRM_ROOT / "checkpoint.json"
PROGRESS = CONFIRM_ROOT / "progress.log"
RESULTS_TSV = ROOT / "experiments" / "power_factor_results" / "confirmation_energy.tsv"
REPORT_MD = ROOT / "experiments" / "PREPARED_POWER_FACTOR_CONFIRMATION.md"

# Confirmation task plan (cfm_id, ap, variant_id, phase)
PHASE1 = [
    ("CFM00_A0_CHIRKOV_BEFORE", "chirkov", 0),
    ("CFM01_B1_SKIP_VALIDATE", "chirkov", 10),
    ("CFM02_B2_DISC_PM_OFF", "chirkov", 11),
    ("CFM03_B3_WIFI_PS_MIN", "chirkov", 12),
    ("CFM04_B7_CPU80", "chirkov", 16),
    ("CFM05_A0_CHIRKOV_AFTER", "chirkov", 0),
    ("CFM06_IO_TEARDOWN_CHIRKOV", "chirkov", 206),
    ("CFM10_A0_AETHERNETIO_BEFORE", "aethernetio", 200),
    ("CFM11_IO_TEARDOWN_AETHERNETIO", "aethernetio", 206),
    ("CFM12_A0_AETHERNETIO_AFTER", "aethernetio", 200),
]

# Combination candidates (selected after phase1 analysis if factors confirm).
COMBO_CANDIDATES = [
    ("CFM20_IO_SKIP", "chirkov", 300),
    ("CFM21_IO_PS_MIN", "chirkov", 301),
    ("CFM22_IO_CPU80", "chirkov", 302),
    ("CFM23_IO_DISC_PM", "chirkov", 303),
]


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " CFM " + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cp() -> dict:
    if not CHECKPOINT.exists():
        return {"run_id": RUN_ID, "task_index": 0, "results": {}, "phase": "phase1"}
    return json.loads(CHECKPOINT.read_text(encoding="utf-8-sig"))


def save_cp(data: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(data, indent=2), encoding="utf-8")


def changed_factors(variant_id: int) -> list[str]:
    factors: list[str] = []
    if variant_id in (0, 1, 200):
        return ["(baseline A0 infrastructure)"]
    if variant_id in pf.SKIP_VALIDATE_VARIANTS or variant_id == 10:
        factors.append("BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP")
    if variant_id in pf.DISC_PM_OFF_VARIANTS:
        factors.append("STA_DISCONNECTED_PM=OFF")
    # Mirror BuildVariant for runtime factors used in confirmation.
    runtime = {
        12: ["WIFI_PS_MIN"],
        16: ["CPU80"],
        206: ["IO_TEARDOWN=DirectDeepSleep(skip wifi stop/deinit)"],
        21: ["DirectDeepSleep"],
        300: ["IO_TEARDOWN", "SKIP_VALIDATE"],
        301: ["IO_TEARDOWN", "WIFI_PS_MIN"],
        302: ["IO_TEARDOWN", "CPU80"],
        303: ["IO_TEARDOWN", "DISC_PM_OFF"],
        310: ["IO_TEARDOWN", "SKIP_VALIDATE", "WIFI_PS_MIN"],
        311: ["IO_TEARDOWN", "SKIP_VALIDATE", "CPU80"],
        312: ["IO_TEARDOWN", "SKIP_VALIDATE", "WIFI_PS_MIN", "CPU80"],
        313: [
            "IO_TEARDOWN",
            "SKIP_VALIDATE",
            "DISC_PM_OFF",
            "WIFI_PS_MIN",
            "CPU80",
        ],
        314: ["FULL_TEARDOWN", "SKIP_VALIDATE", "DISC_PM_OFF", "WIFI_PS_MIN", "CPU80"],
        315: ["SKIP_VALIDATE", "CPU80"],
        316: ["SKIP_VALIDATE", "WIFI_PS_MIN"],
        317: ["SKIP_VALIDATE", "DISC_PM_OFF"],
        318: ["SKIP_VALIDATE", "CPU80", "WIFI_PS_MIN"],
        319: ["SKIP_VALIDATE", "CPU80", "DISC_PM_OFF"],
    }
    factors.extend(runtime.get(variant_id, []))
    # de-dup preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in factors:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out or [f"variant_{variant_id}"]


def isolate_build(cfm_id: str, ap: str, variant_id: int) -> Path:
    build = ROOT / "build-power-confirm" / RUN_ID / cfm_id / ap
    if build.exists():
        shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    pf.BUILD = build
    pf.camp.BUILD = build
    pf.CONFIGURED_MARK = build / "_configured.txt"
    # Always wipe mark so cmake_configure runs.
    if pf.CONFIGURED_MARK.exists():
        pf.CONFIGURED_MARK.unlink()
    return build


def save_build_proof(cfm_id: str, ap: str, variant_id: int, build: Path) -> dict:
    art = CONFIRM_ROOT / RUN_ID / cfm_id
    art.mkdir(parents=True, exist_ok=True)
    sdk = build / "sdkconfig"
    sdk_text = pf.read_text(sdk)
    (art / "effective_sdkconfig.txt").write_text(sdk_text, encoding="utf-8")
    sdk_h = build / "config" / "sdkconfig.h"
    if sdk_h.exists():
        shutil.copy2(sdk_h, art / "sdkconfig.h")
    bin_path = build / "temperature_sensor.bin"
    fw_hash = ""
    if bin_path.exists():
        fw_hash = hashlib.sha256(bin_path.read_bytes()).hexdigest()
        (art / "firmware.sha256").write_text(fw_hash + "\n", encoding="utf-8")
    # Compile defs: AE_POWER_BENCH_VARIANT + key CONFIG_* from sdkconfig.h
    defs: list[str] = [f"AE_POWER_BENCH_VARIANT={variant_id}"]
    if sdk_h.exists():
        for line in sdk_h.read_text(encoding="utf-8", errors="replace").splitlines():
            if "SKIP_VALIDATE" in line or "DISCONNECTED_PM" in line or "DEFAULT_CPU_FREQ" in line:
                if line.startswith("#define "):
                    defs.append(line[len("#define ") :])
    (art / "compile_definitions.txt").write_text("\n".join(defs) + "\n", encoding="utf-8")
    variant_json = {
        "cfm_id": cfm_id,
        "ap": ap,
        "variant_id": variant_id,
        "variant_name": pf.variant_name(variant_id),
        "changed_factors": changed_factors(variant_id),
        "skip_validate": variant_id in pf.SKIP_VALIDATE_VARIANTS,
        "disconnected_pm_enable": variant_id not in pf.DISC_PM_OFF_VARIANTS,
        "teardown_semantics": (
            "DirectDeepSleep: CleanupHotPathWifiRuntime returns immediately; "
            "no handler unregister, no esp_wifi_stop/deinit, no netif destroy, "
            "no event-group delete; deep-sleep armed by bench after TX-done."
            if variant_id in (21, 206, 140, 300, 301, 302, 303, 310, 311, 312, 313)
            else "Full teardown (stop+deinit+netif+handlers) unless StopOnly."
        ),
        "build_dir": str(build),
        "firmware_sha256": fw_hash,
        "sdkconfig_sha256": hashlib.sha256(sdk_text.encode("utf-8")).hexdigest(),
    }
    (art / "variant.json").write_text(json.dumps(variant_json, indent=2), encoding="utf-8")
    return variant_json


def analyze_csv(csv_path: Path) -> dict:
    if not csv_path.exists() or csv_path.stat().st_size < 1000:
        return {"ok": False, "error": "missing_or_tiny_csv"}
    try:
        ts, ua = energy.load_decimated(csv_path)
        cycles = energy.segment_cycles(ts, ua)
        if not cycles:
            return {"ok": False, "error": "no_wakes"}
        e_mj = [c["energy_uJ"] / 1000.0 for c in cycles]
        d_ms = [c["duration_s"] * 1000.0 for c in cycles]
        return {
            "ok": True,
            "n_valid": len(cycles),
            "energy_mean_mJ": statistics.mean(e_mj),
            "energy_median_mJ": statistics.median(e_mj),
            "energy_p90_mJ": energy.pct(e_mj, 0.9),
            "wake_mean_ms": statistics.mean(d_ms),
            "wake_median_ms": statistics.median(d_ms),
            "wake_p90_ms": energy.pct(d_ms, 0.9),
            "first20_wake_median_ms": statistics.median(d_ms[:20]) if len(d_ms) >= 20 else None,
            "last20_wake_median_ms": statistics.median(d_ms[-20:]) if len(d_ms) >= 20 else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def ninja_build_isolated(build: Path, variant_id: int) -> None:
    """Parallel ninja for isolated confirmation builds (camp defaults to -j1)."""
    import os
    import subprocess

    pf.BUILD = build
    pf.camp.BUILD = build
    pf.assert_sdk_matches_variant(variant_id)
    jobs = max(2, (os.cpu_count() or 4) - 1)
    log(f"ninja -j{jobs} in {build}")
    r = subprocess.run(
        [str(pf.camp.NINJA), "-C", str(build), "-j", str(jobs)],
        cwd=str(ROOT),
        env=pf.camp.env(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        err = CONFIRM_ROOT / RUN_ID / "ninja_fail.txt"
        err.parent.mkdir(parents=True, exist_ok=True)
        err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        raise RuntimeError(f"ninja failed rc={r.returncode} log={err}")
    # Confgen may resurrect defaults during the build — re-lock and rebuild once.
    try:
        pf.assert_sdk_matches_variant(variant_id)
    except RuntimeError as exc:
        log(f"post-ninja sdk drift ({exc}); re-lock and rebuild")
        pf.force_sdk_measured(variant_id)
        r2 = subprocess.run(
            [str(pf.camp.NINJA), "-C", str(build), "-j", str(jobs)],
            cwd=str(ROOT),
            env=pf.camp.env(),
            capture_output=True,
            text=True,
        )
        if r2.returncode != 0:
            raise RuntimeError(f"ninja rebuild failed rc={r2.returncode}") from exc
        pf.assert_sdk_matches_variant(variant_id)


def run_one(cfm_id: str, ap: str, variant_id: int) -> dict:
    retryable = {"PPK_CAPTURE_FAILED", "PPK_HOLD_FAILED", "NO_ARM", "TIMEOUT"}
    last: dict = {}
    for attempt in range(1, pf.MAX_PPK_ATTEMPTS + 1):
        last = _run_one_attempt(cfm_id, ap, variant_id, attempt=attempt)
        if last.get("status") not in retryable:
            return last
        log(f"retry {cfm_id} after {last.get('status')} attempt={attempt}")
        pf.camp.ppk_power_off()
        time.sleep(2.0)
    return last


def _run_one_attempt(cfm_id: str, ap: str, variant_id: int, *, attempt: int = 1) -> dict:
    log(
        f"==== {cfm_id} ap={ap} variant={variant_id} "
        f"({pf.variant_name(variant_id)}) attempt={attempt} ===="
    )
    # Rebuild only on first attempt; retries reuse firmware after power-cycle.
    build = pf.BUILD
    if attempt == 1 or not (Path(str(build)) / "temperature_sensor.bin").exists():
        build = isolate_build(cfm_id, ap, variant_id)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        pf.RAW_DIR = RAW_DIR
        pf.cmake_configure_power(ap, variant_id)
        pf.CONFIGURED_MARK.write_text(f"{ap}:v{variant_id}:confirm", encoding="utf-8")
        ninja_build_isolated(build, variant_id)
        save_build_proof(cfm_id, ap, variant_id, build)
    else:
        build = ROOT / "build-power-confirm" / RUN_ID / cfm_id / ap
        pf.BUILD = build
        pf.camp.BUILD = build

    raw_csv = RAW_DIR / f"{cfm_id}_{ap}.csv"
    rx_log = CONFIRM_ROOT / RUN_ID / cfm_id / "rx.log"
    rx_log.parent.mkdir(parents=True, exist_ok=True)
    proof_path = CONFIRM_ROOT / RUN_ID / cfm_id / "variant.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.exists() else {}

    # Reuse run_variant_once but with our csv/rx paths — call internals carefully.
    # Temporarily monkeypatch result paths by wrapping a local copy of flash/measure.
    prod = pf.prod
    camp = pf.camp
    prod.kill_probe_receiver()
    prod.kill_orphan_serial_tails()
    if rx_log.exists():
        rx_log.unlink()
    if raw_csv.exists():
        raw_csv.unlink()

    prod.start_receiver(rx_log)
    camp.ppk_power_off()
    time.sleep(5.0)
    hold = pf.start_ppk_hold()
    if hold is None:
        return {
            "cfm_id": cfm_id,
            "ap": ap,
            "variant_id": variant_id,
            "status": "PPK_HOLD_FAILED",
            "proof": proof,
        }

    camp.flash(erase=True)
    ppk = None
    t0 = time.time()
    status = "TIMEOUT"
    rx_progress: dict = {}
    armed = False
    try:
        last_progress_key = None
        last_progress_at = time.time()
        ticks = 0
        while time.time() - t0 < pf.RUN_TIMEOUT_S:
            ticks += 1
            if ticks % pf.SLOW_POLL_EVERY == 0 and not prod.probe_receiver_alive():
                prod.start_receiver(rx_log, append=True)
            rtext = pf.read_text(rx_log)
            rx_progress = pf.parse_rx_progress(rtext)
            if not armed and rx_progress["bench_arm"]:
                log("BENCH_ARM; start PPK")
                prod.stop_ppk_hold()
                hold = None
                ppk = prod.start_ppk_log(raw_csv)
                if ppk is None:
                    status = "PPK_CAPTURE_FAILED"
                    break
                armed = True
                last_progress_at = time.time()
            if not armed:
                elapsed = time.time() - t0
                stale_hot = rx_progress.get("hot_max_seq", 0) >= 1
                if elapsed >= pf.ARM_TIMEOUT_S or (
                    stale_hot and elapsed >= 60 and not rx_progress["bench_arm"]
                ):
                    status = "NO_ARM"
                    break
            progress_key = (
                rx_progress.get("hot_unique", 0),
                rx_progress.get("hot_max_seq", 0),
            )
            if progress_key != last_progress_key:
                last_progress_key = progress_key
                last_progress_at = time.time()
            if armed and (
                rx_progress.get("bench_done")
                or rx_progress.get("hot_unique", 0) >= pf.K_HOT_ATTEMPTS
                or rx_progress.get("hot_max_seq", 0) >= pf.K_HOT_ATTEMPTS
                or (
                    rx_progress.get("hot_unique", 0) >= pf.K_MIN_RX_UNIQUE
                    and (time.time() - last_progress_at) >= pf.HOT_IDLE_DONE_S
                )
            ):
                status = "OK"
                time.sleep(3)
                break
            time.sleep(pf.POLL_S)
    finally:
        prod.stop_proc(ppk)
        prod.stop_proc(hold)

    energy_stats = analyze_csv(raw_csv)
    unique = int(rx_progress.get("hot_unique", 0) or 0)
    pass_rx = unique >= pf.K_MIN_RX_UNIQUE
    result = {
        "cfm_id": cfm_id,
        "ap": ap,
        "variant_id": variant_id,
        "variant_name": pf.variant_name(variant_id),
        "changed_factors": changed_factors(variant_id),
        "status": status,
        "elapsed_s": int(time.time() - t0),
        "rx_unique": unique,
        "rx_max_seq": rx_progress.get("hot_max_seq", 0),
        "PASS_RX": pass_rx,
        "ppk_csv": str(raw_csv),
        "energy": energy_stats,
        "proof": proof,
    }
    (CONFIRM_ROOT / RUN_ID / cfm_id / "result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    log(
        f"{cfm_id} status={status} RX={unique} "
        f"E_med={energy_stats.get('energy_median_mJ')} "
        f"wake_med={energy_stats.get('wake_median_ms')}"
    )
    return result


def improvement(base: dict, cur: dict, key: str) -> float | None:
    b = (base.get("energy") or {}).get(key)
    c = (cur.get("energy") or {}).get(key)
    if not b or not c or b == 0:
        return None
    return (b - c) / b * 100.0


def avg_baseline(before: dict, after: dict) -> dict:
    be = before.get("energy") or {}
    ae = after.get("energy") or {}
    out = {}
    for k in (
        "energy_mean_mJ",
        "energy_median_mJ",
        "energy_p90_mJ",
        "wake_mean_ms",
        "wake_median_ms",
        "wake_p90_ms",
    ):
        if be.get(k) is not None and ae.get(k) is not None:
            out[k] = 0.5 * (be[k] + ae[k])
    return {"ok": True, **out}


def drift_pct(before: dict, after: dict, key: str = "energy_median_mJ") -> float | None:
    b = (before.get("energy") or {}).get(key)
    a = (after.get("energy") or {}).get(key)
    if not b or not a or b == 0:
        return None
    return abs(a - b) / b * 100.0


def select_combos(results: dict) -> list[tuple[str, str, int]]:
    """Pick combination tasks from confirmed >=~5% single factors."""
    r = results
    before = r.get("CFM00_A0_CHIRKOV_BEFORE")
    after = r.get("CFM05_A0_CHIRKOV_AFTER")
    if not before or not after:
        return []
    base = {"energy": avg_baseline(before, after)}
    confirmed: list[tuple[str, int]] = []
    checks = [
        ("CFM01_B1_SKIP_VALIDATE", 10, "SKIP"),
        ("CFM02_B2_DISC_PM_OFF", 11, "DISC"),
        ("CFM02R_B2_DISC_PM_OFF", 11, "DISC"),
        ("CFM03_B3_WIFI_PS_MIN", 12, "PS"),
        ("CFM04_B7_CPU80", 16, "CPU"),
        # Only chirkov IO vs chirkov baseline. Never compare aethernetio IO to chirkov A0.
        ("CFM06_IO_TEARDOWN_CHIRKOV", 206, "IO"),
    ]
    for cfm, vid, tag in checks:
        cur = r.get(cfm)
        if not cur or cur.get("status") != "OK" or not cur.get("PASS_RX"):
            continue
        imp = improvement(base, cur, "energy_mean_mJ")
        if imp is not None and imp >= 5.0:
            confirmed.append((tag, vid))
            log(f"confirmed factor {tag} mean_improve={imp:.1f}%")
        else:
            log(f"reject/weak factor {tag} mean_improve={imp}")

    tags = {t for t, _ in confirmed}
    tasks: list[tuple[str, str, int]] = []

    # Recover failed aethernetio A0-before (needed for IO delta on that AP).
    ae0 = r.get("CFM10_A0_AETHERNETIO_BEFORE")
    if not ae0 or ae0.get("status") != "OK" or not ae0.get("PASS_RX"):
        tasks.append(("CFM10R_A0_AETHERNETIO_BEFORE", "aethernetio", 200))

    # Re-measure B2 if it failed (sdk lock was broken / NO_ARM).
    b2 = r.get("CFM02_B2_DISC_PM_OFF")
    b2r = r.get("CFM02R_B2_DISC_PM_OFF")
    if (not b2 or b2.get("status") != "OK" or not b2.get("PASS_RX")) and not (
        b2r and b2r.get("status") == "OK" and b2r.get("PASS_RX")
    ):
        tasks.append(("CFM02R_B2_DISC_PM_OFF", "chirkov", 11))

    if "IO" in tags:
        if "SKIP" in tags:
            tasks.append(("CFM20_IO_SKIP", "chirkov", 300))
        if "PS" in tags:
            tasks.append(("CFM21_IO_PS_MIN", "chirkov", 301))
        if "CPU" in tags:
            tasks.append(("CFM22_IO_CPU80", "chirkov", 302))
        if "DISC" in tags:
            tasks.append(("CFM23_IO_DISC_PM", "chirkov", 303))
        if "SKIP" in tags and "PS" in tags:
            tasks.append(("CFM30_IO_SKIP_PS", "chirkov", 310))
        if "SKIP" in tags and "CPU" in tags:
            tasks.append(("CFM31_IO_SKIP_CPU", "chirkov", 311))
        if {"SKIP", "PS", "CPU"} <= tags:
            tasks.append(("CFM32_IO_SKIP_PS_CPU", "chirkov", 312))
        if {"SKIP", "PS", "CPU", "DISC"} <= tags:
            tasks.append(("CFM33_IO_ALL", "chirkov", 313))

    # Non-IO combinations when DirectDeepSleep is harmful on this AP.
    if "SKIP" in tags and "CPU" in tags:
        tasks.append(("CFM40_SKIP_CPU80", "chirkov", 315))
    if "SKIP" in tags and "PS" in tags:
        tasks.append(("CFM41_SKIP_PS_MIN", "chirkov", 316))
    if "SKIP" in tags and "DISC" in tags:
        tasks.append(("CFM42_SKIP_DISC_PM", "chirkov", 317))
    if {"SKIP", "CPU", "PS"} <= tags:
        tasks.append(("CFM43_SKIP_CPU_PS", "chirkov", 318))
    if {"SKIP", "CPU", "DISC"} <= tags:
        tasks.append(("CFM44_SKIP_CPU_DISC", "chirkov", 319))
    return tasks


def write_report(results: dict, run_id: str) -> None:
    rows = []
    before = results.get("CFM00_A0_CHIRKOV_BEFORE")
    after = results.get("CFM05_A0_CHIRKOV_AFTER")
    io_before = results.get("CFM10_A0_AETHERNETIO_BEFORE")
    io_after = results.get("CFM12_A0_AETHERNETIO_AFTER")
    chir_base = avg_baseline(before, after) if before and after else {}
    io_base = avg_baseline(io_before, io_after) if io_before and io_after else {}

    def base_for(ap: str) -> dict:
        return chir_base if ap == "chirkov" else io_base

    lines = [
        "# Prepared Power Factor Confirmation",
        "",
        f"run_id: `{run_id}`",
        "",
        "## Verdict prelude",
        "",
        "Prior 35-variant campaign used one shared `build-esp32c6-pf-fresh` and",
        "`force_sdk_measured` **appended** `CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP`",
        "for B1 without clearing it afterward. That contamination explains nearly",
        "identical ~353–354 ms wakes for B1/B2/B3/B7. Additionally,",
        "`PowerBenchOptions.disconnected_pm` was never applied at runtime; B2 is now",
        "implemented via `CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE`.",
        "",
        "### IO_TEARDOWN semantics",
        "",
        "Variant 206 (`IO_TEARDOWN`) sets `TeardownPolicy::kDirectDeepSleep` — **the",
        "same policy as B12**. After successful TX-done, `CleanupHotPathWifiRuntime(2)`",
        "returns immediately: no handler unregister, no `esp_wifi_stop`, no",
        "`esp_wifi_deinit`, no netif/event-loop teardown. Deep sleep is armed by the",
        "bench. This is **not** a gentle Wi-Fi stop.",
        "",
        "## Main table",
        "",
        "| variant | AP | exact changed factors | RX/100 | E_mean mJ | E_median mJ | E_p90 mJ | wake_mean ms | wake_median ms | wake_p90 ms | delta mean % | delta median % | PASS |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for cfm_id, res in results.items():
        if not isinstance(res, dict) or "energy" not in res:
            continue
        e = res.get("energy") or {}
        ap = res.get("ap", "")
        base = {"energy": base_for(ap)} if base_for(ap) else {}
        dm = improvement(base, res, "energy_mean_mJ") if base.get("energy") else None
        dmed = improvement(base, res, "energy_median_mJ") if base.get("energy") else None
        factors = ",".join(res.get("changed_factors") or [])
        pas = "YES" if res.get("status") == "OK" and res.get("PASS_RX") else "NO"
        row = [
            res.get("cfm_id", cfm_id),
            ap,
            factors,
            str(res.get("rx_unique", "")),
            f"{e.get('energy_mean_mJ', float('nan')):.2f}" if e.get("ok") else "",
            f"{e.get('energy_median_mJ', float('nan')):.2f}" if e.get("ok") else "",
            f"{e.get('energy_p90_mJ', float('nan')):.2f}" if e.get("ok") else "",
            f"{e.get('wake_mean_ms', float('nan')):.1f}" if e.get("ok") else "",
            f"{e.get('wake_median_ms', float('nan')):.1f}" if e.get("ok") else "",
            f"{e.get('wake_p90_ms', float('nan')):.1f}" if e.get("ok") else "",
            f"{dm:.1f}" if dm is not None else "",
            f"{dmed:.1f}" if dmed is not None else "",
            pas,
        ]
        lines.append("| " + " | ".join(row) + " |")
        rows.append(row)

    chir_drift = drift_pct(before, after) if before and after else None
    io_drift = drift_pct(io_before, io_after) if io_before and io_after else None
    lines += [
        "",
        "### Baseline drift",
        "",
        f"- chirkov A0 before/after median drift: "
        f"{chir_drift:.2f}%" if chir_drift is not None else "- chirkov drift: n/a",
        f"- aethernetio A0 before/after median drift: "
        f"{io_drift:.2f}%" if io_drift is not None else "- aethernetio drift: n/a",
        "",
        "### Build isolation audit",
        "",
        "See `experiments/power_factor_results/config_audit/` and per-CFM",
        "`effective_sdkconfig.txt` / `compile_definitions.txt` / `firmware.sha256`.",
        "",
        "BUILD_CONTAMINATION_FOUND=yes (prior campaign). Confirmation uses isolated",
        "`build-power-confirm/<run>/<cfm>/<ap>` directories with set/clear Kconfig.",
        "",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RESULTS_TSV.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "variant",
        "AP",
        "factors",
        "RX",
        "E_mean",
        "E_median",
        "E_p90",
        "wake_mean",
        "wake_median",
        "wake_p90",
        "delta_mean",
        "delta_median",
        "PASS",
    ]
    with RESULTS_TSV.open("w", encoding="utf-8") as f:
        f.write("\t".join(header) + "\n")
        for row in rows:
            f.write("\t".join(row) + "\n")


def battery_estimate(mean_mJ: float) -> dict:
    """CR2 800 mAh idealized lifetime from MEAN energy @ 3.0 V."""
    v = 3.0
    cap_mAh = 800.0
    sleep_uA = 8.0
    charge_mC = mean_mJ / v  # mJ/V = mC
    # average current for period T seconds: I_avg = sleep + charge/(T)
    # charge_mC / T_s = mA if T in seconds? mC/s = mA. Yes.
    out = {}
    for label, period_s in (("1min", 60.0), ("10min", 600.0)):
        i_send_mA = charge_mC / period_s
        i_avg_mA = sleep_uA / 1000.0 + i_send_mA
        hours = cap_mAh / i_avg_mA if i_avg_mA > 0 else 0
        days = hours / 24.0
        out[label] = {
            "mean_energy_mJ": mean_mJ,
            "charge_per_send_mC": charge_mC,
            "i_avg_mA": i_avg_mA,
            "days": days,
            "months": days / 30.4,
            "years": days / 365.25,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from-checkpoint", action="store_true")
    parser.add_argument("--audit-only", action="store_true", help="run config audit only")
    parser.add_argument("--phase", choices=("phase1", "combo", "all"), default="all")
    parser.add_argument("--run-id", default="", help="reuse/override run id")
    args = parser.parse_args()

    global RUN_ID
    if args.run_id:
        RUN_ID = args.run_id

    if args.audit_only:
        from audit_power_factor_config import main as audit_main

        return audit_main()

    tasks = list(PHASE1)
    if args.dry_run:
        for t in tasks + COMBO_CANDIDATES:
            print("\t".join(map(str, t)))
        return 0

    if not pf.prod.acquire_run_lock():
        return 4
    try:
        CONFIRM_ROOT.mkdir(parents=True, exist_ok=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        pf.prod.kill_orphan_serial_tails()
        pf.prod.build_receiver()

        cp = load_cp() if args.from_checkpoint else {"run_id": RUN_ID, "task_index": 0, "results": {}, "phase": "phase1"}
        if not args.from_checkpoint:
            cp["run_id"] = RUN_ID
        else:
            RUN_ID = cp.get("run_id", RUN_ID)
        results: dict = cp.get("results", {})
        start = int(cp.get("task_index", 0)) if args.from_checkpoint else 0

        if args.phase in ("phase1", "all"):
            for i, (cfm, ap, vid) in enumerate(tasks):
                if i < start:
                    continue
                results[cfm] = run_one(cfm, ap, vid)
                save_cp({"run_id": RUN_ID, "task_index": i + 1, "results": results, "phase": "phase1"})
            start = 0
            cp["phase"] = "combo"

        # Drift gate
        d = drift_pct(
            results.get("CFM00_A0_CHIRKOV_BEFORE", {}),
            results.get("CFM05_A0_CHIRKOV_AFTER", {}),
        )
        if d is not None and d > 5.0:
            log(f"UNSTABLE drift={d:.2f}% — stopping before combinations")
            write_report(results, RUN_ID)
            return 5

        if args.phase in ("combo", "all"):
            combos = select_combos(results)
            # Pick best after pairs: run selected combos then best on both APs.
            for j, (cfm, ap, vid) in enumerate(combos):
                results[cfm] = run_one(cfm, ap, vid)
                save_cp(
                    {
                        "run_id": RUN_ID,
                        "task_index": len(tasks) + j + 1,
                        "results": results,
                        "phase": "combo",
                    }
                )

            # Choose best by mean energy among PASS combo+IO on chirkov.
            candidates = []
            for cfm, res in results.items():
                if res.get("ap") != "chirkov":
                    continue
                if res.get("status") != "OK" or not res.get("PASS_RX"):
                    continue
                e = (res.get("energy") or {}).get("energy_mean_mJ")
                if e is None:
                    continue
                if cfm.startswith("CFM00") or cfm.startswith("CFM05"):
                    continue
                candidates.append((e, cfm, res))
            if candidates:
                candidates.sort()
                best_e, best_cfm, best_res = candidates[0]
                log(f"best chirkov so far {best_cfm} mean={best_e:.2f} mJ")
                # Repeat best on chirkov
                rep = f"{best_cfm}_REPEAT"
                results[rep] = run_one(rep, "chirkov", int(best_res["variant_id"]))
                # Test best on aethernetio
                ae = f"{best_cfm}_AETHERNETIO"
                results[ae] = run_one(ae, "aethernetio", int(best_res["variant_id"]))
                save_cp({"run_id": RUN_ID, "task_index": 999, "results": results, "phase": "done"})

        write_report(results, RUN_ID)
        # Battery from best chirkov mean if present
        best_mean = None
        for res in results.values():
            if res.get("ap") == "chirkov" and res.get("PASS_RX") and (res.get("energy") or {}).get("ok"):
                m = res["energy"]["energy_mean_mJ"]
                if best_mean is None or m < best_mean:
                    best_mean = m
        if best_mean is not None:
            bat = battery_estimate(best_mean)
            (CONFIRM_ROOT / RUN_ID / "battery.json").write_text(
                json.dumps(bat, indent=2), encoding="utf-8"
            )
        pf.prod.kill_probe_receiver()
        log("confirmation complete")
        return 0
    finally:
        pf.prod.release_run_lock()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: {exc}")
        raise
