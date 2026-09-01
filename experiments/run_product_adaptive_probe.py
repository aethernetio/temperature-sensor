#!/usr/bin/env python3
"""Product adaptive Wi-Fi probe campaign: same firmware, two access points.

The firmware selects its own profile, PRE delay and POST delay, so this runner
only flashes it, keeps the TCP console receiver alive and watches the run to
completion. Nothing about the parameters is passed in.

Per access point:
  1. configure + build the product firmware (phase "P")
  2. erase-flash, so the AP association cache starts empty
  3. start the aether probe_receiver over TCP
  4. tail the serial log and the receiver log until stage 9 (DONE)
  5. start PPK2 current logging when the hot run begins, stop it at the end

State lives in experiments/product_probe_checkpoint.json so an interrupted run
resumes at the access point that did not finish.

AP credentials come from run_adaptive_wifi_probe_campaign.APS; none are added
here.
"""

from __future__ import annotations

import argparse
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
OUT = ROOT / "experiments" / "product_adaptive_probe_results"
CHECKPOINT = ROOT / "experiments" / "product_probe_checkpoint.json"
REPORT = ROOT / "experiments" / "ADAPTIVE_WIFI_PROBE_REPORT.md"

AETHER = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")
RX_BUILD = AETHER / "build-probe-receiver"
RX_EXE = RX_BUILD / "probe-receiver.exe"
RX_CONFIG = AETHER / "examples" / "probe_receiver" / "user_config.h"
MINGW_BIN = Path(r"C:/msys64/ucrt64/bin")
MINGW_CXX = MINGW_BIN / "c++.exe"

APS = ("chirkov", "aethernetio")

# The firmware prints one P_STAGE line per non-measured stage, so the highest
# stage seen is the progress indicator. Stages 2, 4 and 7 are silent.
STAGE_DONE = 9
# Whole-run budget and the stall window that triggers a hard reset.
AP_TIMEOUT_S = 3 * 60 * 60
STALL_S = 20 * 60
MAX_HARD_RESETS = 3

spec = importlib.util.spec_from_file_location(
    "camp", ROOT / "experiments" / "run_adaptive_wifi_probe_campaign.py"
)
camp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(camp)


def log(msg: str) -> None:
    camp.log(f"PROD {msg}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

RUN_LOCK = ROOT / "experiments" / "product_probe_runner.pid"


def pid_alive(pid: int) -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
    )
    return str(pid) in (r.stdout or "")


def acquire_run_lock() -> bool:
    """Two concurrent runners corrupt the shared ESP build tree."""
    RUN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(RUN_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                owner = int(RUN_LOCK.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                RUN_LOCK.unlink(missing_ok=True)
                continue
            if owner == os.getpid() or not pid_alive(owner):
                RUN_LOCK.unlink(missing_ok=True)
                continue
            log(f"another runner owns the build tree (pid={owner}); exiting")
            return False
    return False


def release_run_lock() -> None:
    try:
        RUN_LOCK.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


def load_checkpoint() -> dict:
    if not CHECKPOINT.exists():
        return {"ap_index": 0, "results": {}}
    return json.loads(CHECKPOINT.read_text(encoding="utf-8-sig"))


def save_checkpoint(data: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------


def receiver_env() -> dict:
    """Host environment for the receiver: no ESP toolchain, msys64 present."""
    e = os.environ.copy()
    if camp.CPM_SOURCE_CACHE.is_dir():
        e["CPM_SOURCE_CACHE"] = str(camp.CPM_SOURCE_CACHE)
    extra = [
        str(camp.CMAKE.parent),
        str(camp.NINJA.parent),
        r"C:\Program Files\Git\cmd",
        r"C:\Program Files\Git\usr\bin",
    ]
    if MINGW_BIN.is_dir():
        extra.insert(0, str(MINGW_BIN))
    # The wrapper saves the PATH it started with; the ESP-flavoured PATH breaks
    # the host compiler.
    base = e.get("AE_HOST_PATH") or e.get("Path", "")
    tail = [p for p in base.split(";") if p and "riscv32-esp-elf" not in p.lower()]
    e["Path"] = ";".join(dict.fromkeys(extra + tail))
    e.pop("CC", None)
    e.pop("CXX", None)
    return e


def receiver_up_to_date() -> bool:
    if not RX_EXE.exists():
        return False
    built = RX_EXE.stat().st_mtime
    src_dir = AETHER / "examples" / "probe_receiver"
    return all(p.stat().st_mtime <= built for p in src_dir.glob("*"))


def build_receiver() -> None:
    if receiver_up_to_date():
        log("probe_receiver up to date")
        return
    log("build TCP probe_receiver")
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    cfg = [
        str(camp.CMAKE),
        "-S",
        str(AETHER),
        "-B",
        str(RX_BUILD),
        "-G",
        "Ninja",
        f"-DCPM_SOURCE_CACHE={camp.CPM_SOURCE_CACHE.as_posix()}",
        f"-DUSER_CONFIG:PATH={RX_CONFIG.as_posix()}",
        f"-DCMAKE_MAKE_PROGRAM={camp.NINJA.as_posix()}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DAE_BUILD_EXAMPLES=ON",
        "-DAE_BUILD_TESTS=OFF",
        "-DAE_DISTILLATION=OFF",
        "-DAE_FILTRATION=ON",
    ]
    if MINGW_CXX.exists():
        cfg.append(f"-DCMAKE_CXX_COMPILER={MINGW_CXX.as_posix()}")
        cfg.append(f"-DCMAKE_C_COMPILER={(MINGW_BIN / 'gcc.exe').as_posix()}")
    r = subprocess.run(cfg, env=receiver_env(), capture_output=True, text=True)
    if r.returncode != 0:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "rx_cmake.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError(f"receiver cmake failed: {(r.stderr or r.stdout)[-1500:]}")
    camp.kill_build_procs()
    r = subprocess.run(
        [str(camp.NINJA), "-C", str(RX_BUILD), "probe-receiver"],
        env=receiver_env(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "rx_ninja.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("receiver ninja failed")
    if not RX_EXE.exists():
        raise RuntimeError("receiver binary missing")
    # Sidecar the MinGW runtime so launching does not need msys64 on PATH.
    if MINGW_BIN.is_dir():
        for name in ("libgcc_s_seh-1.dll", "libwinpthread-1.dll", "libstdc++-6.dll"):
            src = MINGW_BIN / name
            if src.exists():
                shutil.copy2(src, RX_BUILD / name)
    log("probe_receiver build ok")


# camp.kill_receiver/receiver_alive target temperature_receiver.exe, so this
# campaign needs its own pair or every poll would look like a dead receiver and
# leave a new one behind on the same state directory.
RX_IMAGE = "probe-receiver.exe"


def kill_probe_receiver() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", RX_IMAGE], capture_output=True, text=True
    )
    time.sleep(1)


def probe_receiver_alive() -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {RX_IMAGE}"],
        capture_output=True,
        text=True,
    )
    return RX_IMAGE in (r.stdout or "")


def start_receiver(rx_log: Path, *, append: bool = False) -> None:
    kill_probe_receiver()
    session = camp.PREPARED_RX_SESSION
    (session / "state").mkdir(parents=True, exist_ok=True)
    e = receiver_env()
    e["AE_RECEIVER_SESSION_DIR"] = str(session)
    err = rx_log.with_suffix(".err")
    mode = "a" if append and rx_log.exists() else "w"
    with rx_log.open(mode, encoding="utf-8") as outf, err.open(
        mode, encoding="utf-8"
    ) as errf:
        subprocess.Popen(
            [str(RX_EXE)], cwd=str(session), env=e, stdout=outf, stderr=errf
        )
    t0 = time.time()
    while time.time() - t0 < 180:
        text = read_text(rx_log)
        uid = parse_rx_uid(text)
        if uid and ("RX_TRANSPORT=TCP" in text or "RX_TCP_LINK_UP" in text):
            if uid != camp.SERVICE_UID.lower():
                raise RuntimeError(f"receiver UID mismatch: {uid}")
            log(f"receiver ready TCP uid={uid}")
            return
        if "RX_TRANSPORT=UDP" in text:
            raise RuntimeError("receiver negotiated UDP; TCP-only config expected")
        time.sleep(2)
    raise RuntimeError("receiver TCP not ready")


def parse_rx_uid(text: str) -> str | None:
    m = re.search(r"RX_READY uid=([0-9a-fA-F-]{36})", text)
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------
# Serial tail and PPK
# ---------------------------------------------------------------------------


SERIAL_TAIL_MARK = "ae-product-probe-serial-tail"


def kill_orphan_serial_tails() -> None:
    """A killed runner leaves its reader holding the port, which blocks flash."""
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine "
            f"-match '{SERIAL_TAIL_MARK}' " + "} | ForEach-Object { "
            "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        capture_output=True,
        text=True,
    )
    time.sleep(1)


def start_serial_tail(port: str, out_path: Path) -> subprocess.Popen:
    """Background serial reader that appends to out_path and survives resets."""
    script = f"""
# {SERIAL_TAIL_MARK}
import serial, time
port={port!r}
out={str(out_path)!r}
f=open(out,'a',encoding='utf-8')
ser=None
while True:
    if ser is None:
        try:
            ser=serial.Serial(port, 115200, timeout=0.3)
        except Exception:
            time.sleep(1.0)
            continue
    try:
        line=ser.readline()
    except Exception:
        try:
            ser.close()
        except Exception:
            pass
        ser=None
        time.sleep(1.0)
        continue
    if line:
        f.write(line.decode('utf-8','replace'))
        f.flush()
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.touch()
    return subprocess.Popen(
        [str(camp.PY), "-c", script],
        env=camp.env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def start_ppk_log(csv_path: Path) -> subprocess.Popen | None:
    """Current logging for the hot run. Optional: absence is not a failure."""
    ppk_py = camp.PPK_PY
    script = ROOT / "experiments" / "ppk2_log_power.py"
    if not ppk_py.exists() or not script.exists():
        log("PPK_CAPTURE_REQUIRED: ppk2 venv or logger missing")
        return None
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = csv_path.with_suffix(".log")
    try:
        with log_path.open("w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [
                    str(ppk_py),
                    str(script),
                    "--voltage-mv",
                    str(camp.PPK_VOLTAGE_MV),
                    "--out",
                    str(csv_path),
                ],
                stdout=lf,
                stderr=subprocess.STDOUT,
            )
    except Exception as exc:  # noqa: BLE001
        log(f"PPK_CAPTURE_REQUIRED: launch failed {exc}")
        return None
    time.sleep(5)
    if proc.poll() is not None:
        log("PPK_CAPTURE_REQUIRED: logger exited immediately")
        return None
    log(f"PPK logging at {camp.PPK_VOLTAGE_MV} mV -> {csv_path.name}")
    return proc


def wait_for_board(timeout_s: float = 1800) -> str | None:
    """The board is powered through the PPK2, so assert power while waiting."""
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


def hard_reset(port: str) -> None:
    log(f"hard reset {port}")
    try:
        subprocess.run(
            [str(camp.PY), "-m", "esptool", "--chip", "esp32c6", "-p", port, "run"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"hard reset failed: {exc}")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def highest_stage(serial_text: str) -> int:
    best = -1
    for m in re.finditer(r"P_STAGE stage=(\d+)", serial_text):
        best = max(best, int(m.group(1)))
    return best


def parse_serial(text: str) -> dict:
    out: dict = {"icmp_trials": [], "verdicts": [], "reprobes": []}
    for m in re.finditer(
        r"P_ICMP profile=(-?\d+) pre=(\d+) connects=(\d+) ok=(\d+) icmp_s=(\d+) "
        r"icmp_r=(\d+) loss_ppt=(\d+) mean_ms=(\d+) pass=(\d)",
        text,
    ):
        out["icmp_trials"].append(
            {
                "profile": int(m.group(1)),
                "pre_ms": int(m.group(2)),
                "connects": int(m.group(3)),
                "connect_ok": int(m.group(4)),
                "icmp_sent": int(m.group(5)),
                "icmp_recv": int(m.group(6)),
                "loss_ppt": int(m.group(7)),
                "connect_mean_ms": int(m.group(8)),
                "pass": bool(int(m.group(9))),
            }
        )
    m = re.search(r"P_ICMP_WINNER profile=(-?\d+) pre=(\d+)", text)
    if m:
        out["winner_profile"] = int(m.group(1))
        out["winner_pre_ms"] = int(m.group(2))
    for m in re.finditer(
        r"P_VERDICT stage=(\d+) post=(\d+) unique=(\d+) expected=(\d+) "
        r"verdict=(\d+) batches=(\d+) timeout=(\d)",
        text,
    ):
        out["verdicts"].append(
            {
                "stage": int(m.group(1)),
                "post_ms": int(m.group(2)),
                "unique": int(m.group(3)),
                "expected": int(m.group(4)),
                "verdict": int(m.group(5)),
                "batches": int(m.group(6)),
                "query_timeout": bool(int(m.group(7))),
            }
        )
    for m in re.finditer(r"P_REPROBE reason=(\S+) count=(\d+)", text):
        out["reprobes"].append({"reason": m.group(1), "count": int(m.group(2))})
    stages = list(
        re.finditer(
            r"P_STAGE stage=(\d+) name=(\S+) tag=(\S+) session=(\S+) profile=(\d+) "
            r"pre=(\d+) post=(\d+) sleep=(\d+)",
            text,
        )
    )
    if stages:
        last = stages[-1]
        out["final"] = {
            "stage": int(last.group(1)),
            "name": last.group(2),
            "session": last.group(4),
            "profile": int(last.group(5)),
            "pre_ms": int(last.group(6)),
            "post_ms": int(last.group(7)),
            "sleep_ms": int(last.group(8)),
        }
    out["highest_stage"] = highest_stage(text)
    return out


def parse_rx(text: str) -> dict:
    out: dict = {"probe_results": [], "hot_data": 0}
    for m in re.finditer(
        r"PROBE_RESULT session=(\d+) batch=(\d+) param=(\d+) stage=(\S+) "
        r"profile=(\d+) pre=(\d+) post=(\d+) sleep=(\d+) expected=(\d+) "
        r"unique=(\d+) dup=(\d+) missing=(\d+)",
        text,
    ):
        out["probe_results"].append(
            {
                "batch": int(m.group(2)),
                "param": int(m.group(3)),
                "stage": m.group(4),
                "profile": int(m.group(5)),
                "pre_ms": int(m.group(6)),
                "post_ms": int(m.group(7)),
                "sleep_ms": int(m.group(8)),
                "expected": int(m.group(9)),
                "unique": int(m.group(10)),
                "dup": int(m.group(11)),
                "missing": int(m.group(12)),
            }
        )
    hot = [m for m in re.finditer(r"HOT_DATA session=(\d+) batch=(\d+) seq=(\d+)", text)]
    out["hot_data"] = len(hot)
    out["hot_unique_seq"] = len({m.group(3) for m in hot})
    # Only successful previous sends carry meaningful timing.
    cycles = [
        int(m.group(1))
        for m in re.finditer(
            r"prev_valid=1 prev_status=1 prev_connect_us=\d+ prev_cycle_us=(\d+)",
            text,
        )
    ]
    if cycles:
        cycles.sort()
        out["cycle_us"] = {
            "n": len(cycles),
            "min": cycles[0],
            "median": cycles[len(cycles) // 2],
            "p90": cycles[min(len(cycles) - 1, int(len(cycles) * 0.9))],
            "max": cycles[-1],
        }
    summaries = list(
        re.finditer(
            r"HOT_SUMMARY session=(\d+) batch=(\d+) param=(\d+) profile=(\d+) "
            r"pre=(\d+) post=(\d+) sleep=(\d+) hot_sent=(\d+) hot_fail=(\d+) "
            r"reprobe=(\d+)",
            text,
        )
    )
    if summaries:
        m = summaries[-1]
        out["summary"] = {
            "profile": int(m.group(4)),
            "pre_ms": int(m.group(5)),
            "post_ms": int(m.group(6)),
            "sleep_ms": int(m.group(7)),
            "hot_sent": int(m.group(8)),
            "hot_fail": int(m.group(9)),
            "reprobe": int(m.group(10)),
        }
    return out


# ---------------------------------------------------------------------------
# One access point
# ---------------------------------------------------------------------------


CONFIGURED_MARK = ROOT / "experiments" / "product_probe_configured.txt"


def build_firmware(ap: str) -> None:
    # Reconfiguring rewrites the AP credentials into the build, which costs a
    # near-full rebuild, so it only runs when the target AP actually changes.
    want = f"P:{ap}"
    have = CONFIGURED_MARK.read_text(encoding="utf-8").strip() if (
        CONFIGURED_MARK.exists()
    ) else ""
    if have != want:
        camp.cmake_configure(ap, "P", {"BENCH_CLIENT_ID": "reliability_full_v1"})
        CONFIGURED_MARK.write_text(want, encoding="utf-8")
    else:
        log(f"cmake up to date for {ap}")
    camp.ninja_build()


def run_ap(ap: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    serial_log = OUT / f"{ap}_serial.log"
    rx_log = OUT / f"{ap}_rx.log"
    ppk_csv = OUT / f"{ap}_hot100_power.csv"
    for p in (serial_log, rx_log):
        if p.exists():
            p.unlink()

    log(f"==== {ap} ====")
    build_firmware(ap)
    port = wait_for_board()
    if not port:
        raise RuntimeError("no COM for erase-flash")
    port = camp.flash(erase=True)
    start_receiver(rx_log)
    tail = start_serial_tail(port, serial_log)
    ppk: subprocess.Popen | None = None
    ppk_required = False

    t0 = time.time()
    last_len = -1
    last_progress = time.time()
    resets = 0
    stage = -1
    status = "TIMEOUT"
    try:
        while time.time() - t0 < AP_TIMEOUT_S:
            if not probe_receiver_alive():
                log("receiver dead - restart")
                start_receiver(rx_log, append=True)
            stext = read_text(serial_log)
            rtext = read_text(rx_log)
            new_stage = highest_stage(stext)
            if new_stage > stage:
                stage = new_stage
                log(f"{ap} stage={stage}")
            # The hot run is the only stage worth a current trace.
            if stage >= 7 and ppk is None and not ppk_required:
                ppk = start_ppk_log(ppk_csv)
                ppk_required = ppk is None
            if stage >= STAGE_DONE or "HOT_SUMMARY" in rtext:
                status = "OK"
                # Let the trailing HOT_SUMMARY line land in the receiver log.
                time.sleep(10)
                break
            total = len(stext) + len(rtext)
            if total != last_len:
                last_len = total
                last_progress = time.time()
            elif time.time() - last_progress > STALL_S:
                if resets >= MAX_HARD_RESETS:
                    status = "STALLED"
                    break
                resets += 1
                hard_reset(port)
                last_progress = time.time()
            time.sleep(15)
    finally:
        stop_proc(ppk)
        stop_proc(tail)

    result = {
        "ap": ap,
        "status": status,
        "hard_resets": resets,
        "elapsed_s": int(time.time() - t0),
        "ppk_capture_required": ppk_required,
        "serial": parse_serial(read_text(serial_log)),
        "rx": parse_rx(read_text(rx_log)),
    }
    if ppk_required:
        log("PPK_CAPTURE_REQUIRED")
    (OUT / f"{ap}_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    log(f"{ap} status={status} stage={stage}")
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

REPORT_MARKER = "## PRODUCT ADAPTIVE PROBE"


def render_report(results: dict, rtc_sizeof: int | None) -> str:
    lines = [
        REPORT_MARKER,
        "",
        "One firmware, no hardcoded parameters. The device picks its Wi-Fi",
        "profile, PRE delay and POST delay from its own measurements on",
        "whichever access point it is attached to, then runs a 100-packet hot",
        "campaign with the result.",
        "",
        "Firmware: `main/product_adaptive_wifi_probe.cpp`",
        "(`-DAE_EXP_PRODUCT_ADAPTIVE_PROBE=1`). Receiver:",
        "aether `examples/probe_receiver` (TCP only). Selection algorithm:",
        "`examples/probe_receiver/product_probe_select.h`, host tests in",
        "aether `tests/test-product-probe`.",
        "",
    ]
    if rtc_sizeof is not None:
        lines += [f"`sizeof(ProbeRtcState)` = {rtc_sizeof} bytes of RTC memory.", ""]
    lines += [
        "### Selected parameters",
        "",
        "`HOT sent` and `HOT fail` are the device's own counters: a send fails",
        "only when the local send call fails. `HOT delivered` is how many of",
        "those packets the receiver saw, so the two differ by whatever the",
        "network dropped after the send succeeded.",
        "",
        "| AP | status | profile | PRE ms | POST ms | sleep ms | HOT sent | "
        "HOT fail | HOT delivered | reprobes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for ap in APS:
        r = results.get(ap)
        if not r:
            lines.append(f"| {ap} | not run | - | - | - | - | - | - | - | - |")
            continue
        s = r.get("rx", {}).get("summary") or r.get("serial", {}).get("final") or {}
        lines.append(
            "| {ap} | {st} | P{p} | {pre} | {post} | {sl} | {hs} | {hf} | {hd} "
            "| {rp} |".format(
                ap=ap,
                st=r.get("status", "?"),
                p=s.get("profile", "?"),
                pre=s.get("pre_ms", "?"),
                post=s.get("post_ms", "?"),
                sl=s.get("sleep_ms", "?"),
                hs=s.get("hot_sent", r.get("rx", {}).get("hot_data", "?")),
                hf=s.get("hot_fail", "?"),
                hd=r.get("rx", {}).get("hot_unique_seq", "?"),
                rp=s.get("reprobe", len(r.get("serial", {}).get("reprobes", []))),
            )
        )
    lines += [
        "",
        "### Probe batches as counted by the receiver",
        "",
        "A batch passes when all 20 packets arrive; 19 buys one extra batch at",
        "the same delay. When no candidate passes, the POST delay falls back to",
        "the most conservative value in the table and the stage keeps retrying",
        "it, up to its batch cap, in case it passes later. That is why a table",
        "can show the same POST value repeated without ever reaching 20/20.",
        "",
    ]
    for ap in APS:
        r = results.get(ap)
        if not r:
            continue
        lines += [f"**{ap}**", ""]
        rows = r.get("rx", {}).get("probe_results", [])
        if not rows:
            lines += ["No PROBE_RESULT rows captured.", ""]
            continue
        lines += [
            "| batch | stage | POST ms | expected | unique | dup | missing |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            lines.append(
                "| {b} | {s} | {p} | {e} | {u} | {d} | {m} |".format(
                    b=row["batch"],
                    s=row["stage"],
                    p=row["post_ms"],
                    e=row["expected"],
                    u=row["unique"],
                    d=row["dup"],
                    m=row["missing"],
                )
            )
        cyc = r.get("rx", {}).get("cycle_us")
        if cyc:
            lines += [
                "",
                "Hot cycle time from the previous-send timing carried in each "
                "HOT_DATA packet (us): "
                f"n={cyc['n']} min={cyc['min']} median={cyc['median']} "
                f"p90={cyc['p90']} max={cyc['max']}",
            ]
        if r.get("ppk_capture_required"):
            lines += ["", "**PPK_CAPTURE_REQUIRED** - current trace not captured."]
        else:
            lines += ["", f"Current trace: `{ap}_hot100_power.csv` at 3000 mV."]
        lines.append("")
    lines += [
        "Raw logs and per-AP JSON: `experiments/product_adaptive_probe_results/`.",
        "",
    ]
    return "\n".join(lines)


def write_report(results: dict, rtc_sizeof: int | None) -> None:
    section = render_report(results, rtc_sizeof)
    old = read_text(REPORT)
    if REPORT_MARKER in old:
        head, _, tail = old.partition(REPORT_MARKER)
        # Replace up to the next top-level section, keep the rest of the report.
        rest = tail.split("\n## ", 1)
        remainder = "\n## " + rest[1] if len(rest) > 1 else ""
        REPORT.write_text(head + section + remainder, encoding="utf-8")
    else:
        sep = "" if old.endswith("\n\n") or not old else "\n"
        REPORT.write_text(old + sep + "\n" + section, encoding="utf-8")
    log(f"report section written to {REPORT.name}")


# Pinned by the aether host test test_RtcStateIsCompact; used when the host
# compiler is not reachable from the campaign environment.
RTC_SIZEOF_PINNED = 64


def measure_rtc_sizeof() -> int | None:
    """Compile a two-line probe against the shared header on the host."""
    header = AETHER / "examples" / "probe_receiver" / "product_probe_select.h"
    if not header.exists():
        return None
    if not MINGW_CXX.exists():
        return RTC_SIZEOF_PINNED
    src = OUT / "_rtc_sizeof.cpp"
    exe = OUT / "_rtc_sizeof.exe"
    OUT.mkdir(parents=True, exist_ok=True)
    src.write_text(
        '#include <cstdio>\n#include "product_probe_select.h"\n'
        "int main() { std::printf(\"%zu\\n\", sizeof(ae::probe::ProbeRtcState)); }\n",
        encoding="utf-8",
    )
    # The campaign PATH strips msys64 to protect the ESP toolchain, so the
    # host compiler needs its own bin directory back for this one command.
    e = os.environ.copy()
    e["Path"] = str(MINGW_BIN) + ";" + e.get("Path", "")
    r = subprocess.run(
        [
            str(MINGW_CXX),
            "-std=c++20",
            f"-I{header.parent.as_posix()}",
            str(src),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        env=e,
    )
    if r.returncode != 0:
        (OUT / "_rtc_sizeof.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        return RTC_SIZEOF_PINNED
    r = subprocess.run([str(exe)], capture_output=True, text=True, env=e)
    try:
        return int((r.stdout or "").strip())
    except ValueError:
        (OUT / "_rtc_sizeof.err").write_text(
            f"rc={r.returncode}\nstdout={r.stdout!r}\nstderr={r.stderr!r}\n",
            encoding="utf-8",
        )
        return RTC_SIZEOF_PINNED


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-only", action="store_true", help="compile firmware and receiver only"
    )
    parser.add_argument("--ap", choices=APS, help="run a single access point")
    parser.add_argument(
        "--report-only", action="store_true", help="rewrite the report from saved JSON"
    )
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    camp.OUT = OUT
    rtc_sizeof = measure_rtc_sizeof()
    if rtc_sizeof:
        log(f"sizeof(ProbeRtcState)={rtc_sizeof}")

    if args.report_only:
        results = {}
        for ap in APS:
            p = OUT / f"{ap}_result.json"
            if p.exists():
                results[ap] = json.loads(read_text(p))
        write_report(results, rtc_sizeof)
        return 0

    if not acquire_run_lock():
        return 4
    try:
        kill_orphan_serial_tails()
        if args.build_only:
            build_receiver()
            for ap in APS:
                build_firmware(ap)
                log(f"{ap} firmware build ok")
            log("build-only complete")
            return 0

        cp = load_checkpoint()
        results = cp.get("results", {})
        build_receiver()

        aps = (args.ap,) if args.ap else APS
        start = 0 if args.ap else int(cp.get("ap_index", 0))
        for i, ap in enumerate(aps):
            if not args.ap and i < start:
                continue
            results[ap] = run_ap(ap)
            save_checkpoint({"ap_index": i + 1, "results": results})
            write_report(results, rtc_sizeof)

        kill_probe_receiver()
        write_report(results, rtc_sizeof)
        bad = [ap for ap, r in results.items() if r.get("status") != "OK"]
        log(f"campaign complete; failures={bad or 'none'}")
        return 0 if not bad else 3
    finally:
        release_run_lock()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: {exc}")
        save_checkpoint(load_checkpoint() | {"fatal": str(exc)})
        raise
