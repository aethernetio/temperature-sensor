#!/usr/bin/env python3
"""V2 two-router retake: Test1..5 interleaved (chirkov then aethernetio each).

TCP desktop receiver required. Erase-flash before every firmware variant.
Checkpoint: experiments/adaptive_probe_checkpoint.json
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = ROOT / "experiments" / "adaptive_probe_checkpoint.json"
V2_OUT = ROOT / "experiments" / "adaptive_probe_v2_results"
RX_BUILD = ROOT / "temperature_receiver" / "build-bisect-tcp"
RX_EXE = RX_BUILD / "temperature_receiver.exe"
RX_CONFIG = ROOT / "temperature_receiver" / "user_config_tcp.h"
MINGW_CXX = Path(r"C:/msys64/ucrt64/bin/c++.exe")
MINGW_BIN = Path(r"C:/msys64/ucrt64/bin")

spec = importlib.util.spec_from_file_location(
    "camp", ROOT / "experiments" / "run_adaptive_wifi_probe_campaign.py"
)
camp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(camp)

STEPS = [
    ("TEST1", "chirkov", "icmp"),
    ("TEST1", "aethernetio", "icmp"),
    ("TEST2", "chirkov", "full_ping"),
    ("TEST2", "aethernetio", "full_ping"),
    ("TEST3", "chirkov", "prepared_nosleep"),
    ("TEST3", "aethernetio", "prepared_nosleep"),
    ("TEST4", "chirkov", "prepared_sleep"),
    ("TEST4", "aethernetio", "prepared_sleep"),
    ("TEST5", "chirkov", "long"),
    ("TEST5", "aethernetio", "long"),
]


def log(msg: str) -> None:
    camp.log(f"V2 {msg}")


def log_build_tools() -> None:
    e = camp.env()
    for name, cmd in (
        ("cmake", str(camp.CMAKE)),
        ("ninja", str(camp.NINJA)),
        ("git", e.get("GIT", "git")),
        ("riscv32-esp-elf-g++", str(camp.RISCV_BIN / "riscv32-esp-elf-g++.exe")),
    ):
        log(f"which {name}={cmd}")
    log(f"IDF_PATH={camp.IDF_PATH}")


def save_checkpoint(data: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_checkpoint() -> dict:
    if not CHECKPOINT.exists():
        return {"step_index": 0, "results": {}}
    return json.loads(CHECKPOINT.read_text(encoding="utf-8-sig"))


def wait_for_board(timeout_s: float = 3600) -> str | None:
    log("WAIT_FOR_BOARD")
    t0 = time.time()
    camp.ppk_power_on(settle_s=3.0)
    while time.time() - t0 < timeout_s:
        port = camp.find_port(3)
        if port:
            log(f"board on {port}")
            return port
        if int(time.time() - t0) % 30 < 3:
            camp.ppk_power_on(settle_s=2.0)
        time.sleep(2)
    return None


def flash_erase_always() -> str:
    port = wait_for_board()
    if not port:
        raise RuntimeError("no COM for erase-flash")
    return camp.flash(erase=True)


def receiver_env(*, launch: bool = False) -> dict:
    e = os.environ.copy()
    if camp.CPM_SOURCE_CACHE.is_dir():
        e["CPM_SOURCE_CACHE"] = str(camp.CPM_SOURCE_CACHE)
    extra = [
        str(camp.CMAKE.parent),
        str(camp.NINJA.parent),
        r"C:\Program Files\Git\cmd",
        r"C:\Program Files\Git\usr\bin",
    ]
    if launch and MINGW_BIN.is_dir():
        extra.insert(0, str(MINGW_BIN))
    tail = [p for p in e.get("Path", "").split(";") if p and "riscv32-esp-elf" not in p.lower()]
    e["Path"] = ";".join(dict.fromkeys(extra + tail))
    e.pop("CC", None)
    e.pop("CXX", None)
    return e


def build_receiver_tcp() -> None:
    log("build TCP receiver")
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    cfg = [
        str(camp.CMAKE),
        "-S",
        str(ROOT / "temperature_receiver"),
        "-B",
        str(RX_BUILD),
        "-G",
        "Ninja",
        f"-DCPM_SOURCE_CACHE={camp.CPM_SOURCE_CACHE.as_posix()}",
        f"-DCPM_aether-client-cpp_SOURCE={camp.AETHER}",
        f"-DUSER_CONFIG={RX_CONFIG.as_posix()}",
        f"-DCMAKE_MAKE_PROGRAM={camp.NINJA.as_posix()}",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if MINGW_CXX.exists():
        cfg.append(f"-DCMAKE_CXX_COMPILER={MINGW_CXX.as_posix()}")
        cfg.append(f"-DCMAKE_C_COMPILER={MINGW_CXX.with_name('gcc.exe').as_posix()}")
    r = subprocess.run(cfg, env=receiver_env(), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"receiver cmake failed: {(r.stderr or r.stdout)[-2000:]}")
    camp.kill_build_procs()
    r = subprocess.run(
        [str(camp.NINJA), "-C", str(RX_BUILD), "-j", "1"],
        env=receiver_env(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("receiver ninja failed")
    if not RX_EXE.exists():
        raise RuntimeError("receiver binary missing")
    # Sidecar MinGW runtime DLLs so launch does not need msys64 on PATH.
    if MINGW_BIN.is_dir():
        for name in ("libgcc_s_seh-1.dll", "libwinpthread-1.dll", "libstdc++-6.dll"):
            src = MINGW_BIN / name
            if src.exists():
                shutil.copy2(src, RX_BUILD / name)
    log("TCP receiver build ok")


def verify_receiver_tcp() -> None:
    build_receiver_tcp()
    session = camp.PREPARED_RX_SESSION
    rx_log = V2_OUT / "rx_tcp_verify.log"
    tsv = V2_OUT / "rx_tcp_verify.tsv"
    V2_OUT.mkdir(parents=True, exist_ok=True)
    camp.kill_receiver()
    err = rx_log.with_suffix(".err")
    launch_env = receiver_env(launch=True)
    launch_env["AE_RECEIVER_SESSION_DIR"] = str(session)
    launch_env["AE_DS_TSV"] = str(tsv)
    launch_env["AE_DS_BENCH_TAG"] = "v2_tcp_verify"
    with rx_log.open("w", encoding="utf-8") as outf, err.open("w", encoding="utf-8") as errf:
        proc = subprocess.Popen(
            [str(RX_EXE)],
            cwd=str(session),
            env=launch_env,
            stdout=outf,
            stderr=errf,
        )
    t0 = time.time()
    tcp_ok = False
    uid = None
    while time.time() - t0 < 180:
        if proc.poll() is not None and (time.time() - t0) > 5:
            if proc.returncode not in (None, 0):
                err_text = err.read_text(encoding="utf-8", errors="replace") if err.exists() else ""
                camp.kill_receiver()
                raise RuntimeError(
                    f"receiver exited code={proc.returncode} err={(err_text or 'none')[-500:]}"
                )
        text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
        if "RX_TRANSPORT=TCP" in text or "RX_TCP_LINK_UP" in text:
            tcp_ok = True
        uid = camp.parse_receiver_uid(rx_log)
        if tcp_ok and uid:
            break
        time.sleep(2)
    if not tcp_ok:
        camp.kill_receiver()
        text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
        if "RX_TRANSPORT=UDP" in text and "RX_TRANSPORT=TCP" not in text:
            raise RuntimeError("receiver stuck on UDP — hardware blocked")
        raise RuntimeError("receiver not on TCP — hardware blocked")
    if uid != camp.SERVICE_UID.lower():
        camp.kill_receiver()
        raise RuntimeError(f"receiver UID mismatch: {uid}")
    log(f"RX TCP verified uid={uid}")


def start_v2_receiver(tag: str, tsv: Path, rx_log: Path) -> None:
    camp.kill_receiver()
    session = camp.PREPARED_RX_SESSION
    session.mkdir(parents=True, exist_ok=True)
    if tsv.exists():
        tsv.unlink()
    launch_env = receiver_env(launch=True)
    launch_env["AE_RECEIVER_SESSION_DIR"] = str(session)
    launch_env["AE_DS_TSV"] = str(tsv)
    launch_env["AE_DS_BENCH_TAG"] = tag
    err = rx_log.with_suffix(".err")
    with rx_log.open("w", encoding="utf-8") as outf, err.open("w", encoding="utf-8") as errf:
        subprocess.Popen([str(RX_EXE)], cwd=str(session), env=launch_env, stdout=outf, stderr=errf)
    t0 = time.time()
    while time.time() - t0 < 180:
        text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.exists() else ""
        uid = camp.parse_receiver_uid(rx_log)
        if uid and ("RX_TRANSPORT=TCP" in text or "RX_TCP_LINK_UP" in text):
            if uid != camp.SERVICE_UID.lower():
                raise RuntimeError(f"receiver UID mismatch: {uid}")
            log(f"receiver ready TCP uid={uid}")
            return
        time.sleep(2)
    raise RuntimeError("receiver TCP not ready")


def invalidate_ap_cache(ap: str) -> None:
    """Canonical P0 on first connect after AP switch (erase + fresh flash)."""
    log(f"invalidate AP cache {ap} (erase on next flash)")


def run_test1(ap: str, results: dict) -> None:
    invalidate_ap_cache(ap)
    camp.cmake_configure(ap, "A", {})
    camp.ninja_build()
    flash_erase_always()
    text = camp.capture_until(
        camp.find_port(60) or camp.PORT,
        "A_DONE",
        6 * 60 * 60,
        V2_OUT / f"{ap}_test1_icmp.log",
    )
    parsed = camp.parse_a_winner(text)
    results.setdefault("TEST1", {})[ap] = parsed
    (V2_OUT / f"{ap}_test1_icmp.json").write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    log(f"TEST1 {ap} winner P{parsed.get('winner_profile')} PRE={parsed.get('pre_ms')}")


def run_test2(ap: str, results: dict) -> None:
    invalidate_ap_cache(ap)
    camp.cmake_configure(
        ap,
        "B",
        {
            "AE_PING_CYCLES": "50",
            "AE_RELIABILITY_CLIENT_ID": "reliability_full_v1",
        },
    )
    camp.ninja_build()
    flash_erase_always()
    port = camp.find_port(60) or camp.PORT
    text = camp.capture_until(port, "B_DONE", 4 * 60 * 60, V2_OUT / f"{ap}_test2_full.log")
    parsed = camp.parse_b_sum(text)
    results.setdefault("TEST2", {})[ap] = parsed
    (V2_OUT / f"{ap}_test2_full.json").write_text(json.dumps(parsed, indent=2), encoding="utf-8")


def phase_c_defs(ap: str, results: dict, *, nosleep: bool, sleep_us: int, outer: int, hot: int, post: int, run_id: int) -> dict[str, str]:
    t1 = results.get("TEST1", {}).get(ap, {})
    wp = max(0, int(t1.get("winner_profile", 3)))
    pre = max(50, int(t1.get("pre_ms", 50)))
    return {
        "AE_PROBE_OUTER": str(outer),
        "AE_PROBE_HOT_PER_OUTER": str(hot),
        "AE_PROBE_PROFILE": str(wp),
        "AE_PROBE_PRE_MS": str(pre),
        "AE_PROBE_POST_MS": str(post),
        "AE_PROBE_SLEEP_US": "0" if nosleep else str(sleep_us),
        "AE_PROBE_RUN_ID": str(run_id),
        "BENCH_CLIENT_ID": "reliability_full_v1",
        "AETHER_PREPARED_NONCE_RESERVE": str(hot),
    }


def wait_hot_delivery(tsv: Path, target: int, rx_log: Path, tag: str, timeout_s: int) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if not camp.receiver_alive():
            start_v2_receiver(tag, tsv, rx_log)
        st = camp.analyze_tsv(tsv)
        log(f"{tag} hot={st.get('hot', 0)}/{target}")
        if st.get("hot", 0) >= target:
            return st
        time.sleep(10)
    return camp.analyze_tsv(tsv)


def run_test3(ap: str, results: dict) -> None:
    tsv = V2_OUT / f"{ap}_test3_nosleep.tsv"
    rx_log = V2_OUT / f"{ap}_test3_rx.log"
    start_v2_receiver(f"v2_t3_{ap}", tsv, rx_log)
    camp.cmake_configure(ap, "C", phase_c_defs(ap, results, nosleep=True, sleep_us=0, outer=5, hot=30, post=300, run_id=300))
    camp.ninja_build()
    flash_erase_always()
    baseline = wait_hot_delivery(tsv, 150, rx_log, f"T3_{ap}_base", 45 * 60)
    results.setdefault("TEST3", {})[ap] = {"baseline": baseline, "post": []}
    post_winner = 300
    for post in (200, 100, 50, 25, 10, 0):
        camp.cmake_configure(ap, "C", phase_c_defs(ap, results, nosleep=True, sleep_us=0, outer=1, hot=30, post=post, run_id=10 + post))
        camp.ninja_build()
        flash_erase_always()
        start_v2_receiver(f"v2_t3_{ap}_p{post}", tsv, rx_log)
        st = wait_hot_delivery(tsv, 28, rx_log, f"T3_{ap}_post{post}", 20 * 60)
        results["TEST3"][ap]["post"].append({"post_ms": post, "delivery": st})
        if st.get("hot", 0) >= 28:
            post_winner = post
        else:
            break
    results["TEST3"][ap]["post_winner"] = post_winner


def run_test4(ap: str, results: dict) -> None:
    t3 = results.get("TEST3", {}).get(ap, {})
    post_w = int(t3.get("post_winner", 300))
    tsv = V2_OUT / f"{ap}_test4_sleep.tsv"
    rx_log = V2_OUT / f"{ap}_test4_rx.log"
    sleep_results = []
    for sleep_ms in (1000, 250, 500):
        start_v2_receiver(f"v2_t4_{ap}_s{sleep_ms}", tsv, rx_log)
        camp.cmake_configure(
            ap,
            "C",
            phase_c_defs(ap, results, nosleep=False, sleep_us=sleep_ms * 1000, outer=5, hot=30, post=post_w, run_id=100 + sleep_ms),
        )
        camp.ninja_build()
        camp.flash_after_ppk_power_cycle(erase=True)
        st = wait_hot_delivery(tsv, 150, rx_log, f"T4_{ap}_s{sleep_ms}", 60 * 60)
        sleep_results.append({"sleep_ms": sleep_ms, "delivery": st})
    results.setdefault("TEST4", {})[ap] = sleep_results


def run_test5(ap: str, results: dict) -> None:
    t3 = results.get("TEST3", {}).get(ap, {})
    post_w = int(t3.get("post_winner", 300))
    tsv = V2_OUT / f"{ap}_test5_long.tsv"
    rx_log = V2_OUT / f"{ap}_test5_rx.log"
    start_v2_receiver(f"v2_t5_{ap}_long", tsv, rx_log)
    camp.cmake_configure(
        ap,
        "C",
        phase_c_defs(ap, results, nosleep=False, sleep_us=1_000_000, outer=10, hot=50, post=post_w, run_id=500),
    )
    camp.ninja_build()
    flash_erase_always()
    st = wait_hot_delivery(tsv, 500, rx_log, f"T5_{ap}_long", 3 * 60 * 60)
    results.setdefault("TEST5", {})[ap] = st


def run_step(step_index: int, test: str, ap: str, kind: str, results: dict) -> None:
    log(f"==== STEP {step_index}: {test} {ap} {kind} ====")
    if kind == "icmp":
        run_test1(ap, results)
    elif kind == "full_ping":
        run_test2(ap, results)
    elif kind == "prepared_nosleep":
        run_test3(ap, results)
    elif kind == "prepared_sleep":
        run_test4(ap, results)
    elif kind == "long":
        run_test5(ap, results)
    else:
        raise RuntimeError(f"unknown kind {kind}")


def write_comparison(results: dict) -> None:
    lines = ["# TCP RECEIVER / TWO-ROUTER RETAKE V2\n"]
    for test in ("TEST1", "TEST2", "TEST3", "TEST4", "TEST5"):
        lines.append(f"\n## {test}\n")
        for ap in ("chirkov", "aethernetio"):
            data = results.get(test, {}).get(ap)
            lines.append(f"- **{ap}**: `{json.dumps(data, default=str)[:500]}`\n")
    (V2_OUT / "v2_summary.md").write_text("".join(lines), encoding="utf-8")
    (V2_OUT / "v2_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


def main() -> int:
    V2_OUT.mkdir(parents=True, exist_ok=True)
    camp.OUT = V2_OUT
    camp.RX_EXE = RX_EXE
    log_build_tools()
    cp = load_checkpoint()
    results = cp.get("results", {})
    start = int(cp.get("step_index", 0))
    if start == 0:
        verify_receiver_tcp()
    for i in range(start, len(STEPS)):
        test, ap, kind = STEPS[i]
        try:
            run_step(i, test, ap, kind, results)
        except RuntimeError as exc:
            if "no COM" in str(exc) or "WAIT" in str(exc):
                log(f"board absent at step {i}: {exc}")
                save_checkpoint({"step_index": i, "results": results, "waiting": True})
                return 2
            raise
        save_checkpoint(
            {
                "step_index": i + 1,
                "results": results,
                "last": {"test": test, "router": ap, "kind": kind},
                "rx_transport": "TCP",
                "receiver_uid": camp.SERVICE_UID,
            }
        )
    write_comparison(results)
    camp.kill_receiver()
    log("V2 campaign complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FATAL: {exc}")
        save_checkpoint(load_checkpoint() | {"fatal": str(exc)})
        raise
