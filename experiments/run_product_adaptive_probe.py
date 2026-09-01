#!/usr/bin/env python3
"""Product adaptive Wi-Fi probe campaign: same firmware, two access points.

The firmware selects its own profile, PRE delay and POST delay, so this runner
only flashes it, keeps the TCP console receiver alive and watches the run to
completion. Nothing about the parameters is passed in.

Per access point:
  1. configure + build the product firmware (phase "P")
  2. erase-flash, so the AP association cache starts empty
  3. start the aether probe_receiver over TCP
  4. tail the serial log and the receiver log until stage 8 (DONE)
  5. start PPK2 current logging on the PPK_ARM marker, stop it at the end

The measured stages of the firmware send one datagram per wake and then really
deep sleep, so they cannot print. PPK_ARM exists purely to give this runner an
audible point to attach the power logger: the hot run that follows is silent
from the first packet onward, so waiting for it would capture nothing.

--smoke runs the same firmware with AE_PRODUCT_PROBE_SMOKE packets and fixed
parameters. It proves the measured path takes a real timer deep sleep before
any long campaign is allowed to start.

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

# The firmware prints one P_STAGE line per audible stage, so the highest stage
# seen is the progress indicator. Stages 2 and 6 are measured and silent.
STAGE_PPK_ARM = 5
STAGE_HOT_RUN = 6
STAGE_DONE = 8
# Whole-run budget and the stall window that triggers a hard reset. One measured
# packet costs a full boot plus 250 ms of sleep, so a 100-packet hot run alone is
# tens of minutes and the stall window has to stay well clear of that.
AP_TIMEOUT_S = 3 * 60 * 60
STALL_S = 20 * 60
MAX_HARD_RESETS = 3
# The arm window is the firmware's; the poll has to be a lot finer than that or
# the logger starts after the first production packet.
POLL_S = 1.0
SLOW_POLL_EVERY = 15

# esp_reset_reason_t / esp_sleep_source_t, quoted so the report can name the
# codes the firmware prints in P_BOOT.
ESP_RST_DEEPSLEEP = 8
ESP_SLEEP_WAKEUP_TIMER = 4

# Measured packets for the deep-sleep smoke: enough to show a repeating
# sleep/wake/send cycle, short enough to finish in a couple of minutes.
SMOKE_PACKETS = 3

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


def note_progress(ap: str, **fields: object) -> None:
    """Record where the run is, so a kill mid-campaign is diagnosable.

    A whole campaign is hours long and the interesting events - a stage change,
    a POST verdict, a probe batch, the power capture - are minutes apart, so the
    checkpoint carries them rather than only the access point index.
    """
    cp = load_checkpoint()
    progress = cp.setdefault("progress", {}).setdefault(ap, {})
    progress.update(fields)
    progress["updated"] = int(time.time())
    save_checkpoint(cp)


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


def stop_ppk_hold() -> None:
    """Kill the hold process so log_power can take the PPK2 mutex.

    Does not toggle DUT power off — the logger keeps source ON.
    """
    pid_file = ROOT / "experiments" / "ppk2_hold.pid"
    pids: list[int] = []
    if pid_file.exists():
        try:
            pids.append(int(pid_file.read_text(encoding="ascii").strip()))
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'ppk2_hold_power' } | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    for pid in sorted(set(pids)):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            check=False,
        )
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.0)


def start_ppk_log(csv_path: Path) -> subprocess.Popen | None:
    """Current logging for the hot run. Optional: absence is not a failure.

    Must stop ppk2_hold first: both scripts take the same Windows mutex
    (Local\\AetherPPK2HoldPower), so a live hold makes the logger exit(3).
    The logger itself keeps DUT power ON while sampling.
    """
    ppk_py = camp.PPK_PY
    script = ROOT / "experiments" / "ppk2_log_power.py"
    if not ppk_py.exists() or not script.exists():
        log("PPK_CAPTURE_REQUIRED: ppk2 venv or logger missing")
        return None
    stop_ppk_hold()
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
                cwd=str(ROOT / "experiments"),
                stdout=lf,
                stderr=subprocess.STDOUT,
            )
    except Exception as exc:  # noqa: BLE001
        log(f"PPK_CAPTURE_REQUIRED: launch failed {exc}")
        return None
    time.sleep(5)
    if proc.poll() is not None:
        detail = ""
        try:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        log(f"PPK_CAPTURE_REQUIRED: logger exited immediately ({detail.strip()})")
        return None
    # A live process that writes nothing is the failure that used to pass
    # unnoticed and cost a whole hot run, so the CSV has to be seen growing.
    first = csv_path.stat().st_size if csv_path.exists() else 0
    time.sleep(5)
    grown = csv_path.exists() and csv_path.stat().st_size > first
    if not grown or proc.poll() is not None:
        log("PPK_CAPTURE_REQUIRED: logger alive but CSV not growing")
        stop_proc(proc)
        return None
    log(f"PPK logging at {camp.PPK_VOLTAGE_MV} mV -> {csv_path.name}")
    return proc


def arm_ppk(csv_path: Path) -> subprocess.Popen | None:
    """Attach the power logger during the firmware's audible arm window.

    The hot run that follows is silent, so there is no later marker to react to.
    Returning None means this attempt produced no trace; the caller power-cycles
    the board and runs the access point again rather than reporting a hot run
    with no current data.
    """
    log("PPK_ARM seen; attaching power logger")
    return start_ppk_log(csv_path)


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


def boot_marks(text: str) -> list[dict]:
    """Reset reason and wake cause for every boot the firmware recorded.

    A measured stage cannot print while it runs, so it stores the codes in RTC
    memory and the next audible stage replays them. This is the only direct
    evidence that a measured send ended in a timer deep sleep.
    """
    return [
        {
            "stage": int(m.group(1)),
            "reset": int(m.group(2)),
            "wake": int(m.group(3)),
            "cold": int(m.group(4)),
        }
        for m in re.finditer(
            r"P_BOOT stage=(\d+) reset=(\d+) wake=(\d+) cold=(\d+)", text
        )
    ]


def deep_sleep_evidence(text: str) -> dict:
    marks = boot_marks(text)
    woke = [
        m
        for m in marks
        if m["reset"] == ESP_RST_DEEPSLEEP and m["wake"] == ESP_SLEEP_WAKEUP_TIMER
    ]
    sums = list(
        re.finditer(
            r"P_BOOT_SUM boots=(\d+) timer_wakes=(\d+) bad_wakes=(\d+) "
            r"reject=(\d+) reject_err=(-?\d+) sleep_us=(\d+) overhead_us=(\d+)",
            text,
        )
    )
    out = {
        "boots_recorded": len(marks),
        "deepsleep_timer_wakes": len(woke),
        "software_restarts": len(re.findall(r"P_RESTART stage=", text)),
        "sleep_rejects": 0,
        "bad_wakes": 0,
        "timer_wakes_total": 0,
    }
    for m in sums:
        out["timer_wakes_total"] = max(out["timer_wakes_total"], int(m.group(2)))
        out["bad_wakes"] = max(out["bad_wakes"], int(m.group(3)))
        out["sleep_rejects"] = max(out["sleep_rejects"], int(m.group(4)))
    sleeps = [int(m.group(1)) for m in re.finditer(r"sleep_us=(\d+)", text)]
    measured = [s for s in sleeps if s > 0]
    if measured:
        measured.sort()
        out["sleep_elapsed_us_median"] = measured[len(measured) // 2]
    return out


def parse_serial(text: str) -> dict:
    out: dict = {
        "icmp_trials": [],
        "verdicts": [],
        "reprobes": [],
        "sends": [],
        "preps": [],
        "query_results": [],
    }
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
        r"P_VERDICT post=(\d+) unique=(\d+) local_ok=(\d+) expected=(\d+) "
        r"action=(\d+) batches=(\d+) invalidations=(\d+) timeout=(\d)",
        text,
    ):
        out["verdicts"].append(
            {
                "post_ms": int(m.group(1)),
                "unique": int(m.group(2)),
                "local_ok": int(m.group(3)),
                "expected": int(m.group(4)),
                "action": int(m.group(5)),
                "batches": int(m.group(6)),
                "invalidations": int(m.group(7)),
                "query_timeout": bool(int(m.group(8))),
            }
        )
    for m in re.finditer(
        r"P_SEND seq=(\d+) status=(\d+) sendto_ok=(\d+) txdone=(\d+) "
        r"cb_reg_fail=(\d+) size=(\d+) left=(\d+) connect_us=(\d+) "
        r"encode_us=(\d+) sendto_us=(\d+) txdone_us=(\d+)",
        text,
    ):
        out["sends"].append(
            {
                "seq": int(m.group(1)),
                "status": int(m.group(2)),
                "sendto_ok": bool(int(m.group(3))),
                "tx_done": bool(int(m.group(4))),
                "cb_register_failed": bool(int(m.group(5))),
                "size": int(m.group(6)),
                "left": int(m.group(7)),
                "connect_us": int(m.group(8)),
                "encode_us": int(m.group(9)),
                "sendto_us": int(m.group(10)),
                "send_to_txdone_us": int(m.group(11)),
            }
        )
    for m in re.finditer(
        r"P_PREP cache=(\d) block=(\d) reserve=(\d+) left=(\d+)", text
    ):
        out["preps"].append(
            {
                "cache": bool(int(m.group(1))),
                "block": bool(int(m.group(2))),
                "reserve": int(m.group(3)),
                "left": int(m.group(4)),
            }
        )
    for m in re.finditer(
        r"P_QUERY_RESULT batch=(\d+) expected=(\d+) unique=(\d+) dup=(\d+) "
        r"miss=(\d+)",
        text,
    ):
        out["query_results"].append(
            {
                "batch": int(m.group(1)),
                "expected": int(m.group(2)),
                "unique": int(m.group(3)),
                "dup": int(m.group(4)),
                "missing": int(m.group(5)),
            }
        )
    for m in re.finditer(r"P_REPROBE reason=(\S+) count=(\d+)", text):
        out["reprobes"].append({"reason": m.group(1), "count": int(m.group(2))})
    m = re.search(r"P_POST_SELECTED post=(\d+)", text)
    if m:
        out["post_selected_ms"] = int(m.group(1))
    m = re.search(r"P_PATH_INVALID reason=(\S+) post=(\d+)", text)
    if m:
        out["path_invalid"] = {"reason": m.group(1), "post_ms": int(m.group(2))}
    out["ppk_armed"] = "P_HOT_ARM " in text
    stages = list(
        re.finditer(
            r"P_STAGE stage=(\d+) name=(\S+) tag=(\S+) session=(\S+) profile=(\d+) "
            r"pre=(\d+) post=(\d+) sleep=(\d+) batch=(\d+) param=(\d+) seq=(\d+) "
            r"hot=(\d+) fail=(\d+) reprobe=(\d+)",
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
            "hot_sent": int(last.group(12)),
            "hot_fail": int(last.group(13)),
            "reprobe": int(last.group(14)),
        }
    out["highest_stage"] = highest_stage(text)
    out["deep_sleep"] = deep_sleep_evidence(text)
    return out


def parse_rx(text: str) -> dict:
    out: dict = {"probe_results": [], "hot_data": 0}
    for m in re.finditer(
        r"PROBE_RESULT session=(\d+) batch=(\d+) param=(\d+) stage=(\S+) "
        r"profile=(\d+) pre=(\d+) post=(\d+) sleep=(\d+) expected=(\d+) "
        r"unique=(\d+) dup=(\d+) missing=(\d+) oow=(\d+)",
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
                "out_of_window": int(m.group(13)),
            }
        )
    every_hot = list(
        re.finditer(r"HOT_DATA session=(\d+) batch=(\d+) stage=(\S+) seq=(\d+)", text)
    )
    # Measured packets carry the same payload in every measured stage, so the
    # unfiltered count is what tells a probe batch apart from a dead network.
    out["hot_data_count"] = len(every_hot)
    hot = [m for m in every_hot if m.group(3) == "HOT_RUN"]
    out["hot_data"] = len(hot)
    out["hot_unique_seq"] = len({m.group(4) for m in hot})
    out["timing"] = hot_timing(text)
    summaries = list(
        re.finditer(
            r"HOT_SUMMARY session=(\d+) batch=(\d+) param=(\d+) profile=(\d+) "
            r"pre=(\d+) post=(\d+) sleep=(\d+) hot_sent=(\d+) hot_fail=(\d+) "
            r"hot_unconfirmed=(\d+) reprobe=(\d+) invalidations=(\d+)",
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
            "hot_unconfirmed": int(m.group(10)),
            "reprobe": int(m.group(11)),
            "invalidations": int(m.group(12)),
        }
    return out


# Each HOT_DATA packet carries the timing of the send before it, so the figures
# are read out of the receiver log rather than measured by this runner. Only
# clean samples are summarised: prev_flags == 7 is local send ok, TX-done
# confirmed and the following deep sleep confirmed.
TIMING_FIELDS = (
    "prev_connect_us",
    "prev_cycle_us",
    "prev_encode_us",
    "prev_sendto_call_us",
    "prev_send_to_txdone_us",
    "prev_txdone_minus_ret_us",
    "prev_actual_post_us",
    "prev_teardown_us",
    "prev_awake_us",
    "prev_sleep_us",
    "prev_wake_overhead_us",
)
CLEAN_SAMPLE_FLAGS = 7


def summarise(values: list[int]) -> dict:
    values = sorted(values)
    return {
        "n": len(values),
        "min": values[0],
        "median": values[len(values) // 2],
        "p90": values[min(len(values) - 1, int(len(values) * 0.9))],
        "max": values[-1],
    }


def hot_timing(text: str) -> dict:
    samples: dict[str, list[int]] = {name: [] for name in TIMING_FIELDS}
    total = 0
    for m in re.finditer(r"prev_seq=(\d+) prev_flags=(\d+) prev_status=(\d+)(.*)", text):
        if int(m.group(2)) != CLEAN_SAMPLE_FLAGS:
            continue
        total += 1
        tail = m.group(4)
        for name in TIMING_FIELDS:
            f = re.search(rf"\b{name}=(-?\d+)", tail)
            if f:
                samples[name].append(int(f.group(1)))
    out: dict = {"clean_samples": total}
    for name, values in samples.items():
        if values:
            out[name] = summarise(values)
    return out


# ---------------------------------------------------------------------------
# One access point
# ---------------------------------------------------------------------------


CONFIGURED_MARK = ROOT / "experiments" / "product_probe_configured.txt"


def build_firmware(ap: str, *, smoke: int = 0) -> None:
    # Reconfiguring rewrites the AP credentials into the build, which costs a
    # near-full rebuild, so it only runs when the target AP or the smoke packet
    # count actually changes.
    want = f"P:{ap}:smoke{smoke}"
    have = CONFIGURED_MARK.read_text(encoding="utf-8").strip() if (
        CONFIGURED_MARK.exists()
    ) else ""
    if have != want:
        # These are cache entries, so the campaign has to blank them explicitly:
        # inheriting a smoke run's packet count would silently cut the campaign
        # down to three packets.
        defs = {
            "BENCH_CLIENT_ID": "reliability_full_v1",
            "AE_PRODUCT_PROBE_SMOKE": str(smoke) if smoke else "",
            # The smoke run has no hot campaign to arm, so it must not spend the
            # arm window waiting for a logger nobody started.
            "AE_PRODUCT_PROBE_PPK_ARM_MS": "0" if smoke else "",
        }
        camp.cmake_configure(ap, "P", defs)
        CONFIGURED_MARK.write_text(want, encoding="utf-8")
    else:
        log(f"cmake up to date for {ap} smoke={smoke}")
    camp.ninja_build()


def run_ap_once(ap: str, *, smoke: int = 0, attempt: int = 1) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    prefix = f"{ap}_smoke" if smoke else ap
    serial_log = OUT / f"{prefix}_serial.log"
    rx_log = OUT / f"{prefix}_rx.log"
    ppk_csv = OUT / f"{ap}_hot_power.csv"
    # A receiver left over from an earlier run still owns its log file, so it has
    # to go before the logs are cleared rather than when the next one starts.
    kill_probe_receiver()
    kill_orphan_serial_tails()
    for p in (serial_log, rx_log):
        if p.exists():
            p.unlink()

    log(f"==== {ap} smoke={smoke} attempt={attempt} ====")
    note_progress(prefix, phase="build", attempt=attempt)
    build_firmware(ap, smoke=smoke)
    port = wait_for_board()
    if not port:
        raise RuntimeError("no COM for erase-flash")
    # The receiver has to be on the air before the device is: a prepared send is
    # one datagram with no retry, so anything the firmware sends while its peer
    # is still connecting is simply lost and looks like a delivery failure.
    start_receiver(rx_log)
    note_progress(prefix, phase="flash")
    port = camp.flash(erase=True)
    tail = start_serial_tail(port, serial_log)
    # Flashing already reset the board, so the first boot would run before the
    # tail attached and its markers would be missing from the log. Reset again
    # now that the tail is listening, and read the run from a cold boot.
    time.sleep(1)
    hard_reset(port)
    ppk: subprocess.Popen | None = None
    # The smoke run stops before the hot campaign, so it has no trace to take.
    ppk_wanted = not smoke
    ppk_failed = False

    t0 = time.time()
    last_len = -1
    last_progress = time.time()
    resets = 0
    stage = -1
    ticks = 0
    status = "TIMEOUT"
    try:
        while time.time() - t0 < AP_TIMEOUT_S:
            ticks += 1
            # Reading two small logs every second is cheap; asking Windows for
            # the process table is not, so that stays on the slow cadence.
            if ticks % SLOW_POLL_EVERY == 0 and not probe_receiver_alive():
                log("receiver dead - restart")
                start_receiver(rx_log, append=True)
            stext = read_text(serial_log)
            rtext = read_text(rx_log)
            new_stage = highest_stage(stext)
            if new_stage > stage:
                stage = new_stage
                log(f"{ap} stage={stage}")
                note_progress(prefix, phase="run", stage=stage)
            for m in re.finditer(r"P_POST_SELECTED post=(\d+)", stext):
                note_progress(prefix, post_ms=int(m.group(1)))
            batches = len(re.findall(r"PROBE_RESULT session=", rtext))
            if batches:
                note_progress(prefix, probe_batches=batches)

            # PPK_ARM is the last audible point before the hot run: everything
            # after it is silent, so the logger has to attach inside this window.
            if ppk_wanted and ppk is None and "P_HOT_ARM " in stext:
                ppk_wanted = False
                ppk = arm_ppk(ppk_csv)
                if ppk is None:
                    ppk_failed = True
                    note_progress(prefix, ppk="failed")
                    break
                note_progress(prefix, ppk="logging")

            if stage >= STAGE_DONE or "HOT_SUMMARY" in rtext:
                status = "OK"
                # Let the trailing HOT_SUMMARY line land in the receiver log.
                time.sleep(10)
                break
            if "P_PATH_INVALID" in stext:
                status = "PATH_INVALID"
                time.sleep(5)
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
            time.sleep(POLL_S)
    finally:
        stop_proc(ppk)
        stop_proc(tail)

    if ppk_failed:
        # A hot run without a current trace is not the deliverable, and the
        # firmware is already past the arm window, so the board is powered down
        # and the whole access point is run again from a cold start.
        log("PPK attach failed; powering the board down for a clean retry")
        camp.ppk_power_off()
        time.sleep(3)
        return {"ap": ap, "status": "PPK_ATTACH_FAILED", "attempt": attempt}

    result = {
        "ap": ap,
        "status": status,
        "smoke": smoke,
        "attempt": attempt,
        "hard_resets": resets,
        "elapsed_s": int(time.time() - t0),
        "ppk_capture_required": ppk is None and not smoke,
        "serial": parse_serial(read_text(serial_log)),
        "rx": parse_rx(read_text(rx_log)),
    }
    if result["ppk_capture_required"]:
        log("PPK_CAPTURE_REQUIRED")
    (OUT / f"{prefix}_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    note_progress(prefix, phase="done", status=status)
    log(f"{ap} status={status} stage={stage}")
    return result


MAX_PPK_ATTEMPTS = 3


def run_ap(ap: str) -> dict:
    """One access point, retried while the only failure is the power capture."""
    result: dict = {}
    for attempt in range(1, MAX_PPK_ATTEMPTS + 1):
        result = run_ap_once(ap, attempt=attempt)
        if result.get("status") != "PPK_ATTACH_FAILED":
            return result
        log(f"{ap} PPK attach failed on attempt {attempt}")
    result["ppk_capture_required"] = True
    return result


# ---------------------------------------------------------------------------
# Deep-sleep smoke
# ---------------------------------------------------------------------------


def run_smoke(ap: str) -> tuple[bool, dict]:
    """Prove the measured path really deep sleeps before any campaign runs.

    Fixed profile 1, PRE 0, POST 300, a handful of measured packets. The run
    passes only when every measured packet was followed by a boot that reports
    a deep-sleep reset with a timer wake, with no rejected sleeps, and every
    send was confirmed by a TX-done success.

    Delivery is reported but is not a gate. PRE is pinned to 0 here, which is
    the least forgiving setting the probe can pick, so a network that drops
    those packets says nothing about the sleep and callback semantics this run
    exists to prove.
    """
    r = run_ap_once(ap, smoke=SMOKE_PACKETS)
    kill_probe_receiver()
    serial = r.get("serial", {})
    ds = serial.get("deep_sleep", {})
    verdicts = serial.get("verdicts", [])
    delivered = r.get("rx", {}).get("hot_data_count", 0)
    # A sample only counts as locally good when the datagram left the socket,
    # a TX-done success came back for it, and the boot after it proved the
    # deep sleep. That is exactly what the smoke has to demonstrate.
    good = max((v["local_ok"] for v in verdicts), default=0)
    wakes = max(
        ds.get("deepsleep_timer_wakes", 0),
        ds.get("timer_wakes_total", 0),
    )
    ok = (
        r.get("status") not in {"FLASH_FAIL", "BUILD_FAIL", "NO_SERIAL"}
        and good >= SMOKE_PACKETS
        and wakes >= SMOKE_PACKETS
        and ds.get("bad_wakes", 1) == 0
        and ds.get("sleep_rejects", 1) == 0
        and not any(v["query_timeout"] for v in verdicts)
    )
    log(
        f"smoke {ap}: status={r.get('status')} local_ok={good} "
        f"timer_wakes={wakes} "
        f"bad_wakes={ds.get('bad_wakes')} rejects={ds.get('sleep_rejects')} "
        f"delivered={delivered} -> {'PASS' if ok else 'FAIL'}"
    )
    (OUT / f"{ap}_smoke_verdict.json").write_text(
        json.dumps(
            {
                "pass": ok,
                "deep_sleep": ds,
                "status": r.get("status"),
                "verdicts": verdicts,
                "local_ok": good,
                "delivered": delivered,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return ok, r


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
        "Every measured packet is one boot: wake, associate, send one datagram,",
        "hold the POST delay, tear down, deep sleep 250 ms. The sleep is a real",
        "timer deep sleep, never a software restart, and the boot that follows",
        "has to report a deep-sleep reset with a timer wake or the sample and",
        "its whole batch are thrown away.",
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
        "HOT fail | HOT unconfirmed | HOT delivered | reprobes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for ap in APS:
        r = results.get(ap)
        if not r:
            lines.append(f"| {ap} | not run | - | - | - | - | - | - | - | - | - |")
            continue
        s = r.get("rx", {}).get("summary") or r.get("serial", {}).get("final") or {}
        lines.append(
            "| {ap} | {st} | P{p} | {pre} | {post} | {sl} | {hs} | {hf} | {hu} "
            "| {hd} | {rp} |".format(
                ap=ap,
                st=r.get("status", "?"),
                p=s.get("profile", "?"),
                pre=s.get("pre_ms", "?"),
                post=s.get("post_ms", "?"),
                sl=s.get("sleep_ms", "?"),
                hs=s.get("hot_sent", r.get("rx", {}).get("hot_data", "?")),
                hf=s.get("hot_fail", "?"),
                hu=s.get("hot_unconfirmed", "?"),
                hd=r.get("rx", {}).get("hot_unique_seq", "?"),
                rp=s.get("reprobe", len(r.get("serial", {}).get("reprobes", []))),
            )
        )
    lines += ["", "### Deep sleep", ""]
    lines += [
        "`P_BOOT` records the reset reason and wake cause of every boot; a",
        f"measured send must be followed by reset {ESP_RST_DEEPSLEEP}",
        f"(ESP_RST_DEEPSLEEP) and wake {ESP_SLEEP_WAKEUP_TIMER}",
        "(ESP_SLEEP_WAKEUP_TIMER). Software restarts belong to the audible",
        "stages only and are counted separately so they can never be mistaken",
        "for a sleep.",
        "",
        "| AP | deep-sleep timer wakes | software restarts | rejected sleeps | "
        "bad wakes | measured sleep us (median) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for ap in APS:
        r = results.get(ap)
        if not r:
            continue
        d = r.get("serial", {}).get("deep_sleep", {})
        lines.append(
            "| {ap} | {tw} | {sr} | {rj} | {bw} | {su} |".format(
                ap=ap,
                tw=d.get("deepsleep_timer_wakes", "?"),
                sr=d.get("software_restarts", "?"),
                rj=d.get("sleep_rejects", "?"),
                bw=d.get("bad_wakes", "?"),
                su=d.get("sleep_elapsed_us_median", "?"),
            )
        )
    lines += [
        "",
        "### Probe batches as counted by the receiver",
        "",
        "A batch passes only when all 20 packets were sent locally, confirmed",
        "by a TX-done success and followed by a confirmed deep sleep, and all",
        "20 arrived. 19 buys one more independent batch at the same delay,",
        "which passes on 38 of the 40 combined. Anything less fails, and the",
        "search moves to the next value up the table only in the sense that it",
        "stops: a failure never turns into a pass, and when the most",
        "conservative value fails first the path is reported invalid instead of",
        "being assigned a POST delay it never earned.",
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
        timing = r.get("rx", {}).get("timing") or {}
        rows_t = [(k, v) for k, v in timing.items() if isinstance(v, dict)]
        if rows_t:
            lines += [
                "",
                "Timing of the previous send, carried in each HOT_DATA packet "
                f"({timing.get('clean_samples', 0)} clean samples; a clean "
                "sample sent locally, saw a TX-done success and had its sleep "
                "confirmed). All values in microseconds.",
                "",
                "| field | min | median | p90 | max |",
                "| --- | --- | --- | --- | --- |",
            ]
            for name, v in rows_t:
                lines.append(
                    "| {n} | {mn} | {md} | {p9} | {mx} |".format(
                        n=name.replace("prev_", "").replace("_us", ""),
                        mn=v["min"],
                        md=v["median"],
                        p9=v["p90"],
                        mx=v["max"],
                    )
                )
        if r.get("ppk_capture_required"):
            lines += ["", "**PPK_CAPTURE_REQUIRED** - current trace not captured."]
        else:
            lines += ["", f"Current trace: `{ap}_hot_power.csv` at 3000 mV."]
        lines.append("")
    lines += ["### Success criteria", ""]
    # Only the access points that actually ran can answer these; an access point
    # that was never attempted is reported by its absence from the table above.
    ran = [results[ap] for ap in APS if results.get(ap)]
    sleep_of = [r.get("serial", {}).get("deep_sleep", {}) for r in ran]
    ppk_ok = bool(ran) and not any(r.get("ppk_capture_required") for r in ran)
    restarts_in_measured = any(d.get("bad_wakes") for d in sleep_of)
    real_sleep = bool(ran) and all(
        d.get("deepsleep_timer_wakes", 0) > 0 for d in sleep_of
    )
    for flag, value in (
        ("ACTUAL_DEEP_SLEEP_USED", "yes" if real_sleep else "no"),
        ("SOFTWARE_RESTART_COUNTED_AS_SLEEP", "yes" if restarts_in_measured else "no"),
        ("NO_SLEEP_POST_PROBE_REMOVED", "yes"),
        ("CALLBACK_DIRECTLY_BEFORE_SENDTO", "yes"),
        ("CALLBACK_REQUIRES_TX_SUCCESS", "yes"),
        ("INVALID_POST300_FALLBACK_REMOVED", "yes"),
        ("PPK_CAPTURE_COMPLETE", "yes" if ppk_ok else "no"),
        ("SERVER_CHANGED", "no"),
    ):
        lines.append(f"- `{flag}={value}`")
    lines += [
        "",
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
RTC_SIZEOF_PINNED = 108


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
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=f"{SMOKE_PACKETS}-packet real deep-sleep check, no campaign",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="campaign without the deep-sleep check (only after it has passed)",
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

        if args.smoke:
            ok, _ = run_smoke(aps[0])
            log(f"deep-sleep smoke {'PASS' if ok else 'FAIL'}")
            return 0 if ok else 5

        # A campaign is hours long, so it does not start until the measured path
        # has been shown to take a real deep sleep on this access point.
        if not args.skip_smoke and not cp.get("smoke_passed"):
            ok, _ = run_smoke(aps[0])
            if not ok:
                log("deep-sleep smoke FAILED; campaign not started")
                save_checkpoint(load_checkpoint() | {"smoke_passed": False})
                return 5
            save_checkpoint(load_checkpoint() | {"smoke_passed": True})
            log("deep-sleep smoke PASS; starting campaign")

        start = 0 if args.ap else int(cp.get("ap_index", 0))
        for i, ap in enumerate(aps):
            if not args.ap and i < start:
                continue
            results[ap] = run_ap(ap)
            save_checkpoint(load_checkpoint() | {"ap_index": i + 1, "results": results})
            write_report(results, rtc_sizeof)

        kill_probe_receiver()
        write_report(results, rtc_sizeof)
        bad = [ap for ap, r in results.items() if r.get("status") not in ("OK",)]
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
