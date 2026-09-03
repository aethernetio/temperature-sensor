#!/usr/bin/env python3
"""Prepared low-power BEST-config campaign (STOP-only teardown matrix).

Isolated builds under:
  build-power-best/<run>/<task>/<ap>/

Phases:
  p0_matrix  — P0 FULL control, P1 STOP_FULL_SAFE, P2 STOP_MINIMAL on both APs
  p3         — STOP_DISCONNECT if P1/P2 show association pathology
  ablation   — peel SKIP/CPU/STOP when BEST is not better than P0
  cpu        — retune CPU under winning STOP policy
  encode     — optional encode_during_association vs BEST
  long1000   — 1000 HOT @ 2 s on both APs for portable winner
  sleep60    — 30×60 s e2e RX check (no mandatory PPK)
  all        — run phases adaptively

Does not re-enable rejected DISC_PM_OFF / WIFI_PS_MIN / DirectDeepSleep.
Does not touch the server. Raw PPK CSVs stay untracked.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import batch_energy_report as energy  # noqa: E402
import run_prepared_power_factor_study as pf  # noqa: E402

RUN_ID = time.strftime("%Y%m%d_%H%M%S")
BEST_ROOT = ROOT / "experiments" / "power_best_config"
RAW_DIR = ROOT / "experiments" / "power_modes_raw" / "best_config"
CHECKPOINT = BEST_ROOT / "checkpoint.json"
PROGRESS = BEST_ROOT / "progress.log"
CAMPAIGN_LOCK = BEST_ROOT / "campaign.lock"
RESULTS_TSV = ROOT / "experiments" / "power_factor_results" / "best_config.tsv"
REPORT_MD = ROOT / "experiments" / "PREPARED_POWER_BEST_CONFIG.md"

# Short matrix: SKIP+CPU80 + teardown variants.
P0_MATRIX = [
    ("P0_FULL_CHIRKOV", "chirkov", 400),
    ("P1_STOP_SAFE_CHIRKOV", "chirkov", 401),
    ("P2_STOP_MIN_CHIRKOV", "chirkov", 402),
    ("P0_FULL_AETHERNETIO", "aethernetio", 400),
    ("P1_STOP_SAFE_AETHERNETIO", "aethernetio", 401),
    ("P2_STOP_MIN_AETHERNETIO", "aethernetio", 402),
]

P3_TASKS = [
    ("P3_STOP_DISC_CHIRKOV", "chirkov", 403),
    ("P3_STOP_DISC_AETHERNETIO", "aethernetio", 403),
]

ABLATION = [
    ("A_SKIP_CPU80_STOP", "chirkov", 500),
    ("B_SKIP_CPU160_STOP", "chirkov", 501),
    ("C_NOSKIP_CPU80_STOP", "chirkov", 502),
    ("D_SKIP_CPU80_FULL", "chirkov", 503),
    ("E_SKIP_CPU160_FULL", "chirkov", 504),
    ("F_NOSKIP_CPU80_FULL", "chirkov", 505),
]

CPU_STOP_MIN = [
    ("CPU160_STOP_MIN", "chirkov", 510),
    ("CPU120_STOP_MIN", "chirkov", 511),
    ("CPU80_STOP_MIN", "chirkov", 512),
    ("CPU40_STOP_MIN", "chirkov", 513),
]

CPU_STOP_SAFE = [
    ("CPU160_STOP_SAFE", "chirkov", 531),
    ("CPU120_STOP_SAFE", "chirkov", 532),
    ("CPU80_STOP_SAFE", "chirkov", 530),
    ("CPU40_STOP_SAFE", "chirkov", 533),
]


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " BEST " + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cp() -> dict:
    if not CHECKPOINT.exists():
        return {
            "run_id": RUN_ID,
            "phase": "p0_matrix",
            "results": {},
            "winner": None,
            "hot_attempts": pf.K_HOT_ATTEMPTS,
            "hot_sleep_ms": pf.K_HOT_SLEEP_MS,
        }
    return json.loads(CHECKPOINT.read_text(encoding="utf-8-sig"))


def save_cp(data: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(data, indent=2), encoding="utf-8")


def teardown_name(variant_id: int) -> str:
    mapping = {
        400: "FULL",
        401: "STOP_FULL_SAFE",
        402: "STOP_MINIMAL",
        403: "STOP_DISCONNECT",
        500: "STOP_MINIMAL",
        501: "STOP_MINIMAL",
        502: "STOP_MINIMAL",
        503: "FULL",
        504: "FULL",
        505: "FULL",
        510: "STOP_MINIMAL",
        511: "STOP_MINIMAL",
        512: "STOP_MINIMAL",
        513: "STOP_MINIMAL",
        520: "STOP_MINIMAL",
        530: "STOP_FULL_SAFE",
        531: "STOP_FULL_SAFE",
        532: "STOP_FULL_SAFE",
        533: "STOP_FULL_SAFE",
        540: "STOP_FULL_SAFE",
    }
    return mapping.get(variant_id, "UNKNOWN")


def cpu_mhz(variant_id: int) -> int:
    mapping = {
        400: 80,
        401: 80,
        402: 80,
        403: 80,
        500: 80,
        501: 160,
        502: 80,
        503: 80,
        504: 160,
        505: 80,
        510: 160,
        511: 120,
        512: 80,
        513: 40,
        520: 80,
        530: 80,
        531: 160,
        532: 120,
        533: 40,
        540: 80,
    }
    return mapping.get(variant_id, 160)


# Short dir names keep Windows object paths under CMAKE_OBJECT_PATH_MAX and
# avoid ULP ExternalProject try-compile failures on deep trees.
_BUILD_ALIAS = {
    "LONG1000_CHIRKOV": "L1C",
    "LONG1000_AETHERNETIO": "L1A",
    "SLEEP60_CHIRKOV": "S60C",
    "SLEEP60_AETHERNETIO": "S60A",
    "F_NOSKIP_CPU80_FULL": "F",
}


def _build_root() -> Path:
    # Prefer short run stem (YYMMDD_HHMM) under bpb/ to shrink paths.
    short_run = RUN_ID[2:8] + RUN_ID[9:13] if len(RUN_ID) >= 13 else RUN_ID
    return ROOT / "bpb" / short_run


def _build_dir(task_id: str, ap: str) -> Path:
    alias = _BUILD_ALIAS.get(task_id, task_id[:8])
    return _build_root() / alias / ap[:1]


def reclaim_finished_builds(keep_task: str | None = None) -> None:
    """Drop isolated IDF trees after a task finishes so later ninja/ar is not ENOSPC."""
    root = _build_root()
    if not root.exists():
        return
    keep_alias = (
        _BUILD_ALIAS.get(keep_task, keep_task[:8]) if keep_task else None
    )
    for p in root.iterdir():
        if not p.is_dir():
            continue
        if keep_alias and p.name == keep_alias:
            continue
        log(f"reclaim build {p.name}")
        shutil.rmtree(p, ignore_errors=True)


def isolate_build(task_id: str, ap: str) -> Path:
    reclaim_finished_builds(keep_task=task_id)
    build = _build_dir(task_id, ap)
    bin_path = build / "temperature_sensor.bin"
    configured = build / "_configured.txt"
    # Reuse a finished firmware tree after a mid-run kill (avoid 20min -j1 rebuild).
    if bin_path.exists() and configured.exists():
        log(f"reuse existing build {build}")
        pf.BUILD = build
        pf.camp.BUILD = build
        pf.CONFIGURED_MARK = configured
        return build
    if build.exists():
        shutil.rmtree(build, ignore_errors=True)
    if build.exists():
        time.sleep(0.5)
        shutil.rmtree(build)
    build.mkdir(parents=True, exist_ok=True)
    pf.BUILD = build
    pf.camp.BUILD = build
    pf.CONFIGURED_MARK = build / "_configured.txt"
    if pf.CONFIGURED_MARK.exists():
        pf.CONFIGURED_MARK.unlink()
    return build


def save_build_proof(task_id: str, ap: str, variant_id: int, build: Path) -> dict:
    art = BEST_ROOT / RUN_ID / task_id
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
    proof = {
        "task_id": task_id,
        "ap": ap,
        "variant_id": variant_id,
        "variant_name": pf.variant_name(variant_id),
        "teardown": teardown_name(variant_id),
        "cpu_mhz": cpu_mhz(variant_id),
        "skip_validate": variant_id in pf.SKIP_VALIDATE_VARIANTS,
        "disconnected_pm": variant_id not in pf.DISC_PM_OFF_VARIANTS,
        "build_dir": str(build),
        "firmware_sha256": fw_hash,
        "sdkconfig_sha256": hashlib.sha256(sdk_text.encode("utf-8")).hexdigest(),
    }
    (art / "variant.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return proof


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
        n = len(d_ms)
        return {
            "ok": True,
            "n_valid": n,
            "energy_mean_mJ": statistics.mean(e_mj),
            "energy_median_mJ": statistics.median(e_mj),
            "energy_p90_mJ": energy.pct(e_mj, 0.9),
            "wake_mean_ms": statistics.mean(d_ms),
            "wake_median_ms": statistics.median(d_ms),
            "wake_p90_ms": energy.pct(d_ms, 0.9),
            "first20_wake_median_ms": statistics.median(d_ms[:20])
            if n >= 20
            else None,
            "last20_wake_median_ms": statistics.median(d_ms[-20:])
            if n >= 20
            else None,
            "wake_growth_pct": (
                (
                    statistics.median(d_ms[-20:]) - statistics.median(d_ms[:20])
                )
                / max(statistics.median(d_ms[:20]), 1e-9)
                * 100.0
                if n >= 40
                else None
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    except Exception:
        return False
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def acquire_campaign_lock() -> None:
    """Refuse to start a second campaign on the same checkpoint/build tree."""
    BEST_ROOT.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    if CAMPAIGN_LOCK.exists():
        try:
            old_pid = int(CAMPAIGN_LOCK.read_text(encoding="utf-8").split()[0])
        except (ValueError, OSError):
            old_pid = 0
        if old_pid and old_pid != my_pid and _pid_alive(old_pid):
            raise SystemExit(
                f"campaign already running pid={old_pid}; not starting a second copy"
            )
    CAMPAIGN_LOCK.write_text(
        f"{my_pid}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{RUN_ID}\n",
        encoding="utf-8",
    )
    atexit.register(release_campaign_lock)


def release_campaign_lock() -> None:
    try:
        if not CAMPAIGN_LOCK.exists():
            return
        owner = int(CAMPAIGN_LOCK.read_text(encoding="utf-8").split()[0])
        if owner == os.getpid():
            CAMPAIGN_LOCK.unlink()
    except (ValueError, OSError):
        return


def _ninja_needs_reconfigure(log_text: str) -> bool:
    t = log_text.lower()
    return (
        "verifyglobs" in t
        or "rebuilding 'build.ninja'" in t
        or "rebuilding `build.ninja`" in t
        or ("not a file:" in t and "cmakefiles" in t)
    )


def _run_ninja(build: Path, jobs: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(pf.camp.NINJA), "-C", str(build), "-j", str(jobs)],
        cwd=str(ROOT),
        env=pf.camp.env(),
        capture_output=True,
        text=True,
    )


def _cmake_reconfigure_build(build: Path) -> None:
    """Regenerate ninja/glob files in an existing isolated build dir."""
    log(f"cmake reconfigure {build}")
    r = subprocess.run(
        [str(pf.camp.CMAKE), "-S", str(ROOT), "-B", str(build)],
        cwd=str(ROOT),
        env=pf.camp.env(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        err = BEST_ROOT / RUN_ID / "cmake_reconfigure_fail.txt"
        err.parent.mkdir(parents=True, exist_ok=True)
        err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
        raise RuntimeError(f"cmake reconfigure failed rc={r.returncode} log={err}")


def ninja_build_isolated(build: Path, variant_id: int) -> None:
    pf.BUILD = build
    pf.camp.BUILD = build
    pf.assert_sdk_matches_variant(variant_id)
    jobs = 1
    log(f"ninja -j{jobs} in {build}")
    r = _run_ninja(build, jobs)
    if r.returncode != 0:
        log_text = (r.stdout or "") + "\n" + (r.stderr or "")
        err = BEST_ROOT / RUN_ID / "ninja_fail.txt"
        err.parent.mkdir(parents=True, exist_ok=True)
        err.write_text(log_text, encoding="utf-8")
        low = log_text.lower()
        if "no space left" in low:
            log("ninja ENOSPC; reclaim finished builds and retry")
            reclaim_finished_builds(keep_task=build.parent.name)
            r = _run_ninja(build, jobs)
            if r.returncode != 0:
                err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
                raise RuntimeError(f"ninja failed after ENOSPC reclaim rc={r.returncode} log={err}")
        elif "sdkconfig.h" in log_text and "No such file" in log_text:
            log("ninja sdkconfig.h restat race; retry in place")
            pf.force_sdk_measured(variant_id)
            r = _run_ninja(build, jobs)
            if r.returncode != 0:
                err.write_text((r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8")
                raise RuntimeError(f"ninja failed after sdkconfig.h retry rc={r.returncode} log={err}")
        else:
            raise RuntimeError(f"ninja failed rc={r.returncode} log={err}")
    try:
        pf.assert_sdk_matches_variant(variant_id)
    except RuntimeError as exc:
        log(f"post-ninja sdk drift ({exc}); re-lock and rebuild")
        pf.force_sdk_measured(variant_id)
        r2 = _run_ninja(build, jobs)
        if r2.returncode != 0:
            raise RuntimeError(f"ninja rebuild failed rc={r2.returncode}") from exc
        pf.assert_sdk_matches_variant(variant_id)


def assert_hot_attempts_in_firmware(build: Path, want_hot: int) -> None:
    """Fail flash if prepared_power_factor_bench was not compiled with want_hot."""
    if want_hot <= 100:
        return
    cc = build / "compile_commands.json"
    if not cc.exists():
        raise RuntimeError(f"missing compile_commands.json in {build}")
    entries = json.loads(cc.read_text(encoding="utf-8"))
    cmd = ""
    for ent in entries:
        f = str(ent.get("file", "")).replace("\\", "/")
        if f.endswith("prepared_power_factor_bench.cpp"):
            cmd = ent.get("command") or " ".join(ent.get("arguments") or [])
            break
    if not cmd:
        raise RuntimeError("prepared_power_factor_bench.cpp not in compile_commands.json")
    token = f"AETHER_POWER_BENCH_HOT_ATTEMPTS={want_hot}"
    if token not in cmd:
        raise RuntimeError(
            f"firmware compile missing {token}; command snippet has "
            f"HOT_ATTEMPTS={'yes' if 'AETHER_POWER_BENCH_HOT_ATTEMPTS=' in cmd else 'no'}"
        )
    log(f"compile OK {token}")


def cmake_configure_best(
    ap: str,
    variant_id: int,
    *,
    hot_attempts: int,
    hot_sleep_ms: int,
    min_rx: int,
) -> None:
    """Configure with explicit hot-attempt / sleep overrides for long runs."""
    # Temporarily override module globals used by cmake_configure_power.
    old_att, old_sleep, old_min = pf.K_HOT_ATTEMPTS, pf.K_HOT_SLEEP_MS, pf.K_MIN_RX_UNIQUE
    pf.K_HOT_ATTEMPTS = hot_attempts
    pf.K_HOT_SLEEP_MS = hot_sleep_ms
    pf.K_MIN_RX_UNIQUE = min_rx
    try:
        extra = {
            "AETHER_POWER_BENCH_HOT_ATTEMPTS": str(hot_attempts),
            "AETHER_POWER_BENCH_HOT_SLEEP_MS": str(hot_sleep_ms),
            "AETHER_POWER_BENCH_MIN_RX_UNIQUE": str(min_rx),
            "AETHER_PREPARED_HOT_SLEEP_MS": str(hot_sleep_ms),
            "AETHER_PREPARED_HOT_SLEEP_SECONDS": str(max(1, hot_sleep_ms // 1000)),
            "AETHER_PREPARED_NONCE_RESERVE": str(hot_attempts + 20),
        }
        pf.cmake_configure_power(ap, variant_id, extra=extra)
    finally:
        pf.K_HOT_ATTEMPTS = old_att
        pf.K_HOT_SLEEP_MS = old_sleep
        pf.K_MIN_RX_UNIQUE = old_min


def pathology(result: dict) -> bool:
    """Association pathology: poor RX, or wake growth first20→last20."""
    if not result.get("PASS_RX"):
        return True
    e = result.get("energy") or {}
    growth = e.get("wake_growth_pct")
    if growth is not None and growth >= 15.0:
        return True
    # Large absolute wakes suggest stale-association stall.
    wake_med = e.get("wake_median_ms")
    if wake_med is not None and wake_med >= 1200:
        return True
    return False


def run_one(
    task_id: str,
    ap: str,
    variant_id: int,
    *,
    hot_attempts: int | None = None,
    hot_sleep_ms: int | None = None,
    capture_ppk: bool = True,
) -> dict:
    attempts = hot_attempts if hot_attempts is not None else pf.K_HOT_ATTEMPTS
    sleep_ms = hot_sleep_ms if hot_sleep_ms is not None else pf.K_HOT_SLEEP_MS
    min_rx = max(1, int(0.9 * attempts))
    retryable = {
        "PPK_CAPTURE_FAILED",
        "PPK_HOLD_FAILED",
        "NO_ARM",
        "TIMEOUT",
        "NINJA_FAILED",
    }
    last: dict = {}
    for attempt in range(1, pf.MAX_PPK_ATTEMPTS + 1):
        last = _run_one_attempt(
            task_id,
            ap,
            variant_id,
            attempt=attempt,
            hot_attempts=attempts,
            hot_sleep_ms=sleep_ms,
            min_rx=min_rx,
            capture_ppk=capture_ppk,
        )
        if last.get("status") not in retryable:
            return last
        log(f"retry {task_id} after {last.get('status')} attempt={attempt}")
        pf.camp.ppk_power_off()
        time.sleep(2.0)
    return last


def _run_one_attempt(
    task_id: str,
    ap: str,
    variant_id: int,
    *,
    attempt: int,
    hot_attempts: int,
    hot_sleep_ms: int,
    min_rx: int,
    capture_ppk: bool,
) -> dict:
    log(
        f"==== {task_id} ap={ap} variant={variant_id} "
        f"({pf.variant_name(variant_id)}) teardown={teardown_name(variant_id)} "
        f"HOT={hot_attempts} sleep={hot_sleep_ms}ms attempt={attempt} ===="
    )
    build = pf.BUILD
    if attempt == 1 or not (Path(str(build)) / "temperature_sensor.bin").exists():
        build = isolate_build(task_id, ap)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        pf.RAW_DIR = RAW_DIR
        want_mark = f"{ap}:v{variant_id}:best:h{hot_attempts}:s{hot_sleep_ms}"
        have_mark = (
            pf.read_text(pf.CONFIGURED_MARK).strip()
            if pf.CONFIGURED_MARK.exists()
            else ""
        )
        reused = (build / "temperature_sensor.bin").exists() and have_mark == want_mark
        if reused:
            log(f"skip cmake/ninja; reusing {build}")
        else:
            try:
                cmake_configure_best(
                    ap,
                    variant_id,
                    hot_attempts=hot_attempts,
                    hot_sleep_ms=hot_sleep_ms,
                    min_rx=min_rx,
                )
            except RuntimeError as exc:
                return {
                    "task_id": task_id,
                    "ap": ap,
                    "variant_id": variant_id,
                    "status": "NINJA_FAILED",
                    "error": str(exc),
                }
            pf.CONFIGURED_MARK.write_text(want_mark, encoding="utf-8")
            try:
                ninja_build_isolated(build, variant_id)
            except RuntimeError as exc:
                log(f"ninja failed ({exc}); wipe, cmake, rebuild once")
                # Force a clean tree on rebuild (disable reuse).
                if build.exists():
                    shutil.rmtree(build, ignore_errors=True)
                build = isolate_build(task_id, ap)
                try:
                    cmake_configure_best(
                        ap,
                        variant_id,
                        hot_attempts=hot_attempts,
                        hot_sleep_ms=hot_sleep_ms,
                        min_rx=min_rx,
                    )
                except RuntimeError as exc_cmake:
                    return {
                        "task_id": task_id,
                        "ap": ap,
                        "variant_id": variant_id,
                        "status": "NINJA_FAILED",
                        "error": str(exc_cmake),
                    }
                pf.CONFIGURED_MARK.write_text(want_mark, encoding="utf-8")
                try:
                    ninja_build_isolated(build, variant_id)
                except RuntimeError as exc2:
                    return {
                        "task_id": task_id,
                        "ap": ap,
                        "variant_id": variant_id,
                        "status": "NINJA_FAILED",
                        "error": str(exc2),
                    }
            try:
                assert_hot_attempts_in_firmware(build, hot_attempts)
            except RuntimeError as exc:
                return {
                    "task_id": task_id,
                    "ap": ap,
                    "variant_id": variant_id,
                    "status": "NINJA_FAILED",
                    "error": str(exc),
                }
            save_build_proof(task_id, ap, variant_id, build)
        if reused:
            try:
                assert_hot_attempts_in_firmware(build, hot_attempts)
            except RuntimeError as exc:
                return {
                    "task_id": task_id,
                    "ap": ap,
                    "variant_id": variant_id,
                    "status": "NINJA_FAILED",
                    "error": str(exc),
                }
            if not (BEST_ROOT / RUN_ID / task_id / "variant.json").exists():
                save_build_proof(task_id, ap, variant_id, build)
    else:
        build = _build_dir(task_id, ap)
        pf.BUILD = build
        pf.camp.BUILD = build

    raw_csv = RAW_DIR / f"{task_id}_{ap}.csv"
    rx_log = BEST_ROOT / RUN_ID / task_id / "rx.log"
    rx_log.parent.mkdir(parents=True, exist_ok=True)
    proof_path = BEST_ROOT / RUN_ID / task_id / "variant.json"
    proof = (
        json.loads(proof_path.read_text(encoding="utf-8"))
        if proof_path.exists()
        else {}
    )

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
    # Always re-power before flash. SLEEP60 uses capture_ppk=False, but DUT power
    # is still required for Espressif USB-JTAG enumeration and the long run.
    hold = pf.start_ppk_hold()
    if hold is None:
        return {
            "task_id": task_id,
            "ap": ap,
            "variant_id": variant_id,
            "status": "PPK_HOLD_FAILED",
            "proof": proof,
        }

    camp.flash(erase=True)
    ppk = None
    t0 = time.time()
    # Scale timeout with attempt count (≈3 s wake + sleep_ms + margin).
    run_timeout = max(
        pf.RUN_TIMEOUT_S,
        int(hot_attempts * (hot_sleep_ms / 1000.0 + 4.0) + 180),
    )
    status = "TIMEOUT"
    rx_progress: dict = {}
    armed = False
    try:
        last_progress_key = None
        last_progress_at = time.time()
        ticks = 0
        while time.time() - t0 < run_timeout:
            ticks += 1
            if ticks % pf.SLOW_POLL_EVERY == 0 and not prod.probe_receiver_alive():
                prod.start_receiver(rx_log, append=True)
            rtext = pf.read_text(rx_log)
            rx_progress = pf.parse_rx_progress(rtext)
            if not armed and rx_progress["bench_arm"]:
                log("BENCH_ARM; start capture" if capture_ppk else "BENCH_ARM")
                if capture_ppk:
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
                rx_progress.get("hot_unique", 0) >= hot_attempts
                or rx_progress.get("hot_max_seq", 0) + 1 >= hot_attempts
                or (
                    # Only honor bench_done when the firmware actually reached
                    # the requested attempt count. parse_rx_progress otherwise
                    # marks done at the module-global K_HOT_ATTEMPTS=100.
                    rx_progress.get("bench_done")
                    and max(
                        int(rx_progress.get("hot_unique", 0) or 0),
                        int(rx_progress.get("hot_max_seq", 0) or 0),
                    )
                    >= int(0.85 * hot_attempts)
                )
                or (
                    hot_attempts <= 100
                    and rx_progress.get("hot_unique", 0) >= min_rx
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

    energy_stats = analyze_csv(raw_csv) if capture_ppk else {"ok": False, "skipped": True}
    unique = int(rx_progress.get("hot_unique", 0) or 0)
    pass_rx = unique >= min_rx
    result = {
        "task_id": task_id,
        "ap": ap,
        "variant_id": variant_id,
        "variant_name": pf.variant_name(variant_id),
        "teardown": teardown_name(variant_id),
        "cpu_mhz": cpu_mhz(variant_id),
        "skip_validate": variant_id in pf.SKIP_VALIDATE_VARIANTS,
        "status": status,
        "elapsed_s": int(time.time() - t0),
        "hot_attempts": hot_attempts,
        "hot_sleep_ms": hot_sleep_ms,
        "rx_unique": unique,
        "rx_max_seq": rx_progress.get("hot_max_seq", 0),
        "loss_pct": (1.0 - unique / float(hot_attempts)) * 100.0 if hot_attempts else None,
        "PASS_RX": pass_rx,
        "pathology": False,
        "ppk_csv": str(raw_csv) if capture_ppk else None,
        "energy": energy_stats,
        "proof": proof,
    }
    result["pathology"] = pathology(result)
    (BEST_ROOT / RUN_ID / task_id / "result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    log(
        f"{task_id} status={status} RX={unique}/{hot_attempts} "
        f"E_mean={energy_stats.get('energy_mean_mJ')} "
        f"wake_med={energy_stats.get('wake_median_ms')} "
        f"pathology={result['pathology']}"
    )
    return result


def mean_e(result: dict | None) -> float | None:
    if not result:
        return None
    e = result.get("energy") or {}
    return e.get("energy_mean_mJ")


def pick_short_winner(results: dict) -> dict | None:
    """Among portable non-pathology PASS teardowns, pick lowest combined MEAN energy.

    Preference order (STOP_MINIMAL → SAFE → DISC → FULL) only breaks ties.
    """
    candidates = [
        ("STOP_MINIMAL", 402, "P2_STOP_MIN_CHIRKOV", "P2_STOP_MIN_AETHERNETIO"),
        ("STOP_FULL_SAFE", 401, "P1_STOP_SAFE_CHIRKOV", "P1_STOP_SAFE_AETHERNETIO"),
        ("STOP_DISCONNECT", 403, "P3_STOP_DISC_CHIRKOV", "P3_STOP_DISC_AETHERNETIO"),
        ("FULL", 400, "P0_FULL_CHIRKOV", "P0_FULL_AETHERNETIO"),
    ]
    scored: list[tuple[float, int, dict]] = []
    pref_rank = {name: i for i, (name, *_rest) in enumerate(candidates)}
    for name, vid, chir, ae in candidates:
        rc = results.get(chir)
        ra = results.get(ae)
        if not rc or not ra:
            continue
        if rc.get("status") != "OK" or ra.get("status") != "OK":
            continue
        if not rc.get("PASS_RX") or not ra.get("PASS_RX"):
            continue
        if rc.get("pathology") or ra.get("pathology"):
            continue
        mc = mean_e(rc)
        ma = mean_e(ra)
        if mc is None or ma is None:
            continue
        scored.append(
            (
                0.5 * (mc + ma),
                pref_rank[name],
                {
                    "teardown": name,
                    "variant_id": vid,
                    "chirkov_task": chir,
                    "aethernetio_task": ae,
                    "chirkov_mean": mc,
                    "aethernetio_mean": ma,
                },
            )
        )
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]


def need_p3(results: dict) -> bool:
    # Only if no STOP_MINIMAL/SAFE portable candidate passed.
    for name, _vid, chir, ae in (
        ("STOP_MINIMAL", 402, "P2_STOP_MIN_CHIRKOV", "P2_STOP_MIN_AETHERNETIO"),
        ("STOP_FULL_SAFE", 401, "P1_STOP_SAFE_CHIRKOV", "P1_STOP_SAFE_AETHERNETIO"),
    ):
        rc = results.get(chir)
        ra = results.get(ae)
        if (
            rc
            and ra
            and rc.get("status") == "OK"
            and ra.get("status") == "OK"
            and rc.get("PASS_RX")
            and ra.get("PASS_RX")
            and not rc.get("pathology")
            and not ra.get("pathology")
        ):
            return False
    return True


def need_ablation(results: dict, winner: dict | None) -> bool:
    """Run ablation when STOP was tested and is not clearly better than FULL."""
    p0c = results.get("P0_FULL_CHIRKOV")
    m0 = mean_e(p0c)
    if m0 is None:
        return True
    stop_tasks = (
        "P1_STOP_SAFE_CHIRKOV",
        "P2_STOP_MIN_CHIRKOV",
        "P3_STOP_DISC_CHIRKOV",
    )
    any_stop_ok = False
    stop_beats_full = False
    for tid in stop_tasks:
        r = results.get(tid)
        if not r or r.get("status") != "OK" or not r.get("PASS_RX"):
            continue
        any_stop_ok = True
        m = mean_e(r)
        if m is not None and m < m0 * 0.97:
            stop_beats_full = True
    if not any_stop_ok:
        return False
    # STOP exists but does not beat FULL → ablate interactions.
    if not stop_beats_full:
        return True
    if not winner:
        return True
    return False


def write_tsv(results: dict) -> None:
    RESULTS_TSV.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "variant",
        "AP",
        "sdkconfig",
        "CPU",
        "teardown",
        "RX/attempts",
        "loss %",
        "E_mean",
        "E_median",
        "E_p90",
        "wake_mean",
        "wake_median",
        "wake_p90",
        "connect failures",
        "reconnects",
        "PASS",
    ]
    lines = ["\t".join(cols)]
    for tid, r in sorted(results.items()):
        e = r.get("energy") or {}
        proof = r.get("proof") or {}
        skip = "SKIP_VALIDATE" if r.get("skip_validate") else "NO_SKIP"
        lines.append(
            "\t".join(
                [
                    tid,
                    str(r.get("ap")),
                    skip,
                    str(r.get("cpu_mhz")),
                    str(r.get("teardown")),
                    f"{r.get('rx_unique')}/{r.get('hot_attempts')}",
                    f"{r.get('loss_pct'):.2f}" if r.get("loss_pct") is not None else "",
                    f"{e.get('energy_mean_mJ'):.3f}" if e.get("energy_mean_mJ") is not None else "",
                    f"{e.get('energy_median_mJ'):.3f}"
                    if e.get("energy_median_mJ") is not None
                    else "",
                    f"{e.get('energy_p90_mJ'):.3f}" if e.get("energy_p90_mJ") is not None else "",
                    f"{e.get('wake_mean_ms'):.1f}" if e.get("wake_mean_ms") is not None else "",
                    f"{e.get('wake_median_ms'):.1f}"
                    if e.get("wake_median_ms") is not None
                    else "",
                    f"{e.get('wake_p90_ms'):.1f}" if e.get("wake_p90_ms") is not None else "",
                    "",
                    "",
                    "YES" if r.get("PASS_RX") else "NO",
                ]
            )
        )
        _ = proof
    RESULTS_TSV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def battery_days(mean_mj: float, period_s: float) -> dict:
    # CR2 800 mAh @ 3.0 V, sleep 8 µA
    v = 3.0
    capacity_c = 0.8 * 3600.0  # A·s
    sleep_a = 8e-6
    charge_c = mean_mj / 1000.0 / v
    i_avg = charge_c / period_s + sleep_a
    life_s = capacity_c / i_avg if i_avg > 0 else float("inf")
    return {
        "charge_mC": charge_c * 1000.0,
        "i_avg_mA": i_avg * 1000.0,
        "life_days": life_s / 86400.0,
    }


def write_report(cp: dict) -> None:
    results = cp.get("results") or {}
    winner = cp.get("winner") or {}
    write_tsv(results)
    lines = [
        "# Prepared Power Best Config",
        "",
        f"run_id: `{cp.get('run_id')}`",
        "",
        "Follow-on to `PREPARED_POWER_FACTOR_CONFIRMATION.md` (c2709d5).",
        "Goal: STOP Wi-Fi (`esp_wifi_stop`) without deinit, on top of confirmed",
        "`SKIP_VALIDATE + CPU80`, portable across chirkov and aethernetio.",
        "",
        "## Teardown semantics",
        "",
        "| Policy | Behavior |",
        "|---|---|",
        "| FULL | unregister handlers, stop, deinit, destroy netif, delete event loop |",
        "| STOP_FULL_SAFE | unregister handlers, `esp_wifi_stop`, no deinit |",
        "| STOP_MINIMAL | `esp_wifi_stop` only |",
        "| STOP_DISCONNECT | `esp_wifi_disconnect` (+≤150 ms wait), unregister, stop |",
        "| DIRECT_DEEP_SLEEP | historical only — no stop; AP-dependent; forbidden for portable |",
        "",
        "## Main table",
        "",
        "See `experiments/power_factor_results/best_config.tsv`.",
        "",
    ]
    for tid, r in sorted(results.items()):
        e = r.get("energy") or {}
        lines.append(
            f"- **{tid}** ({r.get('ap')}): status={r.get('status')} "
            f"RX={r.get('rx_unique')}/{r.get('hot_attempts')} "
            f"teardown={r.get('teardown')} CPU={r.get('cpu_mhz')} "
            f"E_mean={e.get('energy_mean_mJ')} "
            f"wake_med={e.get('wake_median_ms')} "
            f"pathology={r.get('pathology')}"
        )
    lines += ["", "## PORTABLE WINNER", ""]
    if winner:
        wc = results.get(winner.get("chirkov_task", ""), {})
        wa = results.get(winner.get("aethernetio_task", ""), {})
        we = wc.get("energy") or {}
        ae = wa.get("energy") or {}
        mean = we.get("energy_mean_mJ") or 0.0
        b1 = battery_days(mean, 60.0)
        b10 = battery_days(mean, 600.0)
        lines += [
            f"SKIP_VALIDATE={winner.get('skip_validate', True)}",
            f"CPU={winner.get('cpu_mhz', cpu_mhz(winner.get('variant_id', 0)))}",
            f"teardown={winner.get('teardown')}",
            "disconnected PM=ON",
            "connected PS=NONE",
            f"encode during association={winner.get('encode', False)}",
            "PRE chirkov=25 ms",
            "PRE aethernetio=0 ms",
            "POST=0",
            "external RTC=yes",
            "console/logging=NONE",
            "",
            "### CHIRKOV",
            f"attempts={wc.get('hot_attempts')} RX unique={wc.get('rx_unique')} "
            f"loss={wc.get('loss_pct')}",
            f"E mean/median/p90={we.get('energy_mean_mJ')}/"
            f"{we.get('energy_median_mJ')}/{we.get('energy_p90_mJ')}",
            f"wake mean/median/p90={we.get('wake_mean_ms')}/"
            f"{we.get('wake_median_ms')}/{we.get('wake_p90_ms')}",
            "",
            "### AETHERNETIO",
            f"attempts={wa.get('hot_attempts')} RX unique={wa.get('rx_unique')} "
            f"loss={wa.get('loss_pct')}",
            f"E mean/median/p90={ae.get('energy_mean_mJ')}/"
            f"{ae.get('energy_median_mJ')}/{ae.get('energy_p90_mJ')}",
            f"wake mean/median/p90={ae.get('wake_mean_ms')}/"
            f"{ae.get('wake_median_ms')}/{ae.get('wake_p90_ms')}",
            "",
            f"NEXT ASSOCIATION CONFLICT: observed={'yes' if (wc.get('pathology') or wa.get('pathology')) else 'no'}",
            f"FULL TEARDOWN NEEDED: {'yes' if winner.get('teardown') == 'FULL' else 'no'}",
            f"esp_wifi_deinit NEEDED BEFORE DEEP SLEEP: {'yes' if winner.get('teardown') == 'FULL' else 'no'}",
            "",
            "### Battery (CR2 800 mAh @ 3.0 V, sleep 8 µA, MEAN energy)",
            f"1 min: I_avg={b1['i_avg_mA']:.3f} mA life≈{b1['life_days']:.1f} d",
            f"10 min: I_avg={b10['i_avg_mA']:.3f} mA life≈{b10['life_days']:.1f} d",
        ]
    else:
        lines.append("Winner not yet selected (campaign in progress).")
    lines += [
        "",
        "## ChatGPT handoff",
        "",
        "```",
        "PREPARED LOW-POWER FINAL",
        f"PORTABLE_WINNER={winner.get('teardown')}",
        f"SDKCONFIG=SKIP_VALIDATE_IN_DEEP_SLEEP + EXT_CRYS + DISC_PM_ON + CONSOLE_NONE",
        f"CPU={winner.get('cpu_mhz')}",
        f"TEARDOWN={winner.get('teardown')}",
        f"DEINIT_BEFORE_SLEEP={'yes' if winner.get('teardown') == 'FULL' else 'no'}",
        "DISCONNECTED_PM=ON",
        "CONNECTED_PS=NONE",
        f"ENCODE_OVERLAP={winner.get('encode', False)}",
        "SERVER_CHANGED=no",
        f"ALL_USEFUL_CHANGES_PUSHED={cp.get('pushed', 'pending')}",
        "```",
        "",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {REPORT_MD}")


def run_tasks(cp: dict, tasks: list[tuple[str, str, int]], **kwargs) -> None:
    want_hot = int(kwargs.get("hot_attempts") or pf.K_HOT_ATTEMPTS)
    for task_id, ap, vid in tasks:
        prev = cp["results"].get(task_id)
        if prev and prev.get("status") == "NINJA_FAILED":
            log(f"skip done {task_id} status=NINJA_FAILED")
            continue
        if prev and prev.get("status") == "OK" and prev.get("PASS_RX", False):
            got = int(prev.get("rx_max_seq") or prev.get("rx_unique") or 0)
            # Reject "OK" long-runs that clearly stopped at the default 100 HOT.
            if want_hot > 100 and got < int(0.85 * want_hot):
                log(
                    f"redo {task_id}: short firmware run got_seq={got} "
                    f"want_hot={want_hot}"
                )
            else:
                log(
                    f"skip done {task_id} status=OK "
                    f"RX={prev.get('rx_unique')}/{got}"
                )
                continue
        elif prev and prev.get("status") in ("OK", "SHORT_RUN") and not prev.get(
            "PASS_RX", False
        ):
            log(f"redo {task_id}: prior {prev.get('status')} PASS_RX=false")
        # Keep module K_HOT_ATTEMPTS aligned for parse_rx_progress bench_done.
        old_att = pf.K_HOT_ATTEMPTS
        if kwargs.get("hot_attempts") is not None:
            pf.K_HOT_ATTEMPTS = int(kwargs["hot_attempts"])
        try:
            cp["results"][task_id] = run_one(task_id, ap, vid, **kwargs)
        finally:
            pf.K_HOT_ATTEMPTS = old_att
        # Guard: long-run firmware must not stop at legacy 100 attempts.
        cur = cp["results"][task_id]
        if (
            cur.get("status") == "OK"
            and want_hot > 100
            and int(cur.get("rx_max_seq") or cur.get("rx_unique") or 0)
            < int(0.85 * want_hot)
        ):
            cur["status"] = "SHORT_RUN"
            cur["PASS_RX"] = False
            cur["pathology"] = True
            log(
                f"{task_id} SHORT_RUN seq={cur.get('rx_max_seq')} "
                f"want={want_hot} (HOT_ATTEMPTS override missing in firmware?)"
            )
        save_cp(cp)
        write_report(cp)
        if cp["results"][task_id].get("status") == "OK":
            reclaim_finished_builds()
        # Isolated IDF trees are ~2.5 GB; drop after measurement so later
        # variants and LONG1000 PPK CSVs do not fill the disk.
        build_tree = _build_dir(task_id, "x").parent
        if build_tree.exists():
            shutil.rmtree(build_tree, ignore_errors=True)
            log(f"freed build tree {task_id}")


def main() -> int:
    global RUN_ID
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        default="all",
        choices=[
            "p0_matrix",
            "p3",
            "ablation",
            "cpu",
            "encode",
            "long1000",
            "sleep60",
            "all",
            "report",
        ],
    )
    ap.add_argument("--from-checkpoint", action="store_true")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.run_id:
        RUN_ID = args.run_id
    if args.from_checkpoint and CHECKPOINT.exists():
        cp = load_cp()
        RUN_ID = cp.get("run_id", RUN_ID)
    else:
        cp = {
            "run_id": RUN_ID,
            "phase": args.phase,
            "results": {},
            "winner": None,
        }

    BEST_ROOT.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        acquire_campaign_lock()
    log(f"start run_id={RUN_ID} phase={args.phase}")

    if args.dry_run:
        for t in P0_MATRIX + P3_TASKS + ABLATION:
            print(t)
        return 0

    if args.phase in ("p0_matrix", "all"):
        cp["phase"] = "p0_matrix"
        save_cp(cp)
        run_tasks(cp, P0_MATRIX)

    prev_winner = dict(cp.get("winner") or {})
    winner = pick_short_winner(cp["results"])
    if args.phase in ("p3", "all") and (
        args.phase == "p3" or need_p3(cp["results"])
    ):
        cp["phase"] = "p3"
        save_cp(cp)
        run_tasks(cp, P3_TASKS)
        winner = pick_short_winner(cp["results"])

    if winner:
        winner["cpu_mhz"] = cpu_mhz(winner["variant_id"])
        winner["skip_validate"] = True
        winner["encode"] = bool(prev_winner.get("encode", False))
        # Preserve validation flags when resuming a later phase from checkpoint.
        if "long1000_pass" in prev_winner:
            winner["long1000_pass"] = prev_winner["long1000_pass"]
        for key in ("cpu_task", "variant_id", "teardown"):
            if key in prev_winner and key not in winner:
                winner[key] = prev_winner[key]
        if prev_winner.get("variant_id") and args.phase in (
            "long1000",
            "sleep60",
            "report",
        ):
            # Prefer the locked checkpoint winner for late-phase resumes.
            winner["variant_id"] = prev_winner["variant_id"]
            winner["teardown"] = prev_winner.get("teardown", winner.get("teardown"))
            winner["cpu_mhz"] = prev_winner.get("cpu_mhz", winner.get("cpu_mhz"))
        cp["winner"] = winner
        save_cp(cp)
    elif prev_winner:
        winner = prev_winner

    if args.phase in ("ablation", "all") and (
        args.phase == "ablation" or need_ablation(cp["results"], winner)
    ):
        cp["phase"] = "ablation"
        save_cp(cp)
        # If STOP_FULL_SAFE won, map ablation STOP ids to SAFE equivalents for A.
        tasks = list(ABLATION)
        if winner and winner.get("teardown") == "STOP_FULL_SAFE":
            tasks = [
                ("A_SKIP_CPU80_STOP", "chirkov", 530),
                ("B_SKIP_CPU160_STOP", "chirkov", 531),
                ("C_NOSKIP_CPU80_STOP", "chirkov", 502),  # no SAFE noskip id; keep min
                ("D_SKIP_CPU80_FULL", "chirkov", 503),
                ("E_SKIP_CPU160_FULL", "chirkov", 504),
                ("F_NOSKIP_CPU80_FULL", "chirkov", 505),
            ]
        run_tasks(cp, tasks)

    if winner and winner.get("teardown") in ("STOP_MINIMAL", "STOP_FULL_SAFE"):
        if args.phase in ("cpu", "all"):
            cp["phase"] = "cpu"
            save_cp(cp)
            cpu_tasks = (
                CPU_STOP_MIN
                if winner.get("teardown") == "STOP_MINIMAL"
                else CPU_STOP_SAFE
            )
            run_tasks(cp, cpu_tasks)
            # Retune CPU by MEAN energy among PASS_RX.
            best_cpu = None
            best_mean = None
            for tid, _, vid in cpu_tasks:
                r = cp["results"].get(tid)
                if not r or not r.get("PASS_RX") or r.get("status") != "OK":
                    continue
                m = mean_e(r)
                if m is None:
                    continue
                if best_mean is None or m < best_mean:
                    best_mean = m
                    best_cpu = (tid, vid, cpu_mhz(vid))
            if best_cpu:
                winner["cpu_mhz"] = best_cpu[2]
                winner["cpu_task"] = best_cpu[0]
                winner["variant_id"] = best_cpu[1]
                cp["winner"] = winner
                save_cp(cp)

        if args.phase in ("encode", "all"):
            cp["phase"] = "encode"
            save_cp(cp)
            enc_vid = 520 if winner.get("teardown") == "STOP_MINIMAL" else 540
            run_tasks(cp, [("ENCODE_OVERLAP", "chirkov", enc_vid)])
            enc = cp["results"].get("ENCODE_OVERLAP")
            base_tid = winner.get("cpu_task") or winner.get("chirkov_task")
            base = cp["results"].get(base_tid)
            if (
                enc
                and enc.get("PASS_RX")
                and mean_e(enc) is not None
                and mean_e(base) is not None
                and mean_e(enc) < mean_e(base) * 0.97
            ):
                winner["encode"] = True
                cp["winner"] = winner
                save_cp(cp)

    if winner and args.phase in ("long1000", "all"):
        cp["phase"] = "long1000"
        save_cp(cp)
        vid = winner["variant_id"]
        # Ensure long-run uses winning teardown+cpu: map to explicit P variants when possible.
        if winner.get("teardown") == "STOP_MINIMAL" and winner.get("cpu_mhz") == 80:
            vid = 402
        elif winner.get("teardown") == "STOP_FULL_SAFE" and winner.get("cpu_mhz") == 80:
            vid = 401
        elif winner.get("teardown") == "STOP_DISCONNECT":
            vid = 403
        elif winner.get("teardown") == "FULL":
            vid = 400
        run_tasks(
            cp,
            [
                ("LONG1000_CHIRKOV", "chirkov", vid),
                ("LONG1000_AETHERNETIO", "aethernetio", vid),
            ],
            hot_attempts=1000,
            hot_sleep_ms=2000,
        )
        for tid in ("LONG1000_CHIRKOV", "LONG1000_AETHERNETIO"):
            r = cp["results"].get(tid)
            if r and (not r.get("PASS_RX") or r.get("pathology")):
                log(f"LONG RUN FAIL {tid}; reject winner")
                winner["long1000_pass"] = False
                break
        else:
            winner["long1000_pass"] = True
        cp["winner"] = winner
        save_cp(cp)

    if winner and winner.get("long1000_pass") and args.phase in ("sleep60", "all"):
        cp["phase"] = "sleep60"
        save_cp(cp)
        vid = winner.get("variant_id", 402)
        run_tasks(
            cp,
            [
                ("SLEEP60_CHIRKOV", "chirkov", vid),
                ("SLEEP60_AETHERNETIO", "aethernetio", vid),
            ],
            hot_attempts=30,
            hot_sleep_ms=60000,
            capture_ppk=False,
        )

    cp["phase"] = "done"
    save_cp(cp)
    write_report(cp)
    log("best-config campaign complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
