#!/usr/bin/env python3
"""Æther prepared HOT 100x / 2 s / PPK full capture + C++ TCP receiver E2E."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402
from ppk_stream_cycle_segmenter import CycleWindow, SegmenterConfig, StreamingCycleSegmenter  # noqa: E402
from run_udp_100x_2s_ppk_fullcapture import (  # noqa: E402
    COLD_BOOT_S,
    FS_HZ,
    GUARD_SAMPLES,
    MEASURED,
    PERIOD_S,
    VOLTAGE_MV,
    VOLTAGE_V,
    agg_stats,
    build_rows,
    integrate_window,
    pct,
    resegment_offline,
)

BUILD = ROOT / "build-aether-hot-100x-2s-ppk"
RX_SRC = ROOT / "temperature_receiver"
RX_BUILD = RX_SRC / "build-aether-hot-tcp-rx-msvc"
RX_EXE: Path | None = None
RX_BUILD_LOG = HERE / "power_factor_results" / "aether_hot_receiver_msvc_build.log"
RESULTS = HERE / "power_factor_results"
LOCAL_RAW = RESULTS / "aether_hot_100x_2s_ppk_tcp_rx_raw"
WINDOWS_DIR = LOCAL_RAW / "windows"
CSV_OUT = RESULTS / "aether_hot_100x_2s_ppk_tcp_rx_sends.csv"
JSON_OUT = RESULTS / "aether_hot_100x_2s_ppk_tcp_rx.json"
MD_OUT = HERE / "AETHER_HOT_100X_2S_PPK_TCP_RX.md"
_RX_STATE_CANDIDATES = (
    HERE / "prepared_wifi_cache_rx_session",
    HERE / "reliability_rx_session",
    HERE / "aether_hot_tcp_rx_session",
)
RX_STATE = next((p for p in _RX_STATE_CANDIDATES if p.is_dir()), HERE / "aether_hot_tcp_rx_session")

START_BRANCH = ""
START_SHA = ""
RE_RX_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_HOT_RX = re.compile(
    r"AETHER_HOT_RX wall_ms=\d+ run_id=(\d+) seq=(\d+) duplicate=(\d+) out_of_order=(\d+)"
)
RE_FULL_READY = re.compile(r"AETHER_FULL_READY run_id=(\d+)")
RE_TCP_OK = re.compile(r"RX_TCP_VERIFIED=yes")
RE_FAIL_TCP = re.compile(r"FAIL_TCP_RECEIVER=")

AETHER_PIN_SHA = "157aadbec8e7b852d0f89274307ff7cb8103e5f7"
PINNED_AETHER_LOCAL = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")


def wifi_creds() -> tuple[str, str]:
    return "chirkov", "kcdjepWz51"


def git_meta() -> tuple[str, str]:
    b = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, cwd=ROOT, check=False)
    s = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=False)
    return (b.stdout or "").strip(), (s.stdout or "").strip()


def _vswhere_path() -> Path:
    pf = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return Path(pf) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"


def _find_vs_installation() -> Path:
    vw = _vswhere_path()
    if not vw.is_file():
        raise RuntimeError(f"vswhere not found: {vw}")
    r = subprocess.run(
        [
            str(vw),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    inst = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not inst or not inst[0].strip():
        raise RuntimeError(
            "No Visual Studio installation with VC.Tools.x86.x64 found. "
            f"vswhere exit={r.returncode} stderr={r.stderr!r}"
        )
    return Path(inst[0].strip())


def _capture_msvc_env(vs_root: Path) -> dict[str, str]:
    vcvars = vs_root / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    vsdev = vs_root / "Common7" / "Tools" / "VsDevCmd.bat"
    bootstrap = vcvars if vcvars.is_file() else vsdev
    if not bootstrap.is_file():
        raise RuntimeError(f"MSVC environment bootstrap not found under {vs_root}")
    # shell=True: cmd.exe list form mangles quoted paths with spaces on Windows.
    inner = f'call "{bootstrap}" >nul 2>&1 && set'
    r = subprocess.run(inner, shell=True, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"VsDevCmd failed exit={r.returncode}\n{r.stderr}")
    env = os.environ.copy()
    for line in (r.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key:
            env[key] = val
    return env


def _log_msvc_toolchain(env: dict[str, str], log: list[str]) -> None:
    print("RECEIVER_COMPILER_REQUESTED=MSVC", flush=True)
    log.append("RECEIVER_COMPILER_REQUESTED=MSVC")
    cl_exe = ""
    for label, args in (
        ("MSVC_CL_PATH", ["where", "cl"]),
        ("CMAKE_PATH", ["where", "cmake"]),
        ("NINJA_PATH", ["where", "ninja"]),
    ):
        wr = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
        path = (wr.stdout or "").strip().splitlines()
        first = path[0] if path else ""
        if label == "MSVC_CL_PATH":
            cl_exe = first
        line = f"{label}={first}"
        print(line, flush=True)
        log.append(line)
    if cl_exe:
        bvr = subprocess.run([cl_exe], env=env, capture_output=True, text=True, check=False)
        ver_line = next((ln for ln in (bvr.stderr or "").splitlines() if "Version" in ln), "")
    else:
        ver_line = ""
    if ver_line:
        print(f"MSVC_CL_VERSION={ver_line.strip()}", flush=True)
        log.append(f"MSVC_CL_VERSION={ver_line.strip()}")
    print("MSVC_ARCH=x64", flush=True)
    log.append("MSVC_ARCH=x64")


def _read_cmake_cache_value(cache_path: Path, key: str) -> str:
    if not cache_path.is_file():
        return ""
    prefix = f"{key}:"
    for line in cache_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[-1].strip()
    return ""


def _assert_msvc_configure(rx_build: Path, log: list[str]) -> None:
    cache = rx_build / "CMakeCache.txt"
    cid = _read_cmake_cache_value(cache, "CMAKE_CXX_COMPILER_ID")
    cpath = _read_cmake_cache_value(cache, "CMAKE_CXX_COMPILER")
    ctype = _read_cmake_cache_value(cache, "CMAKE_BUILD_TYPE")
    log.extend(
        [
            f"RECEIVER_COMPILER_ID={cid}",
            f"RECEIVER_COMPILER_PATH={cpath}",
            f"RECEIVER_BUILD_TYPE={ctype}",
        ]
    )
    print(f"RECEIVER_COMPILER_ID={cid}", flush=True)
    print(f"RECEIVER_COMPILER_PATH={cpath}", flush=True)
    print(f"RECEIVER_BUILD_TYPE={ctype}", flush=True)
    cpath_norm = cpath.replace("\\", "/").lower()
    if cid != "MSVC":
        if cid == "" and cpath_norm.endswith("/cl.exe"):
            cid = "MSVC"
        else:
            raise RuntimeError(f"CMake did not select MSVC (CMAKE_CXX_COMPILER_ID={cid!r})")
    if not cpath_norm.endswith("/cl.exe"):
        raise RuntimeError(f"CMAKE_CXX_COMPILER is not cl.exe: {cpath!r}")


def _resolve_rx_exe(rx_build: Path) -> Path:
    for candidate in (
        rx_build / "aether_hot_tcp_receiver.exe",
        rx_build / "Release" / "aether_hot_tcp_receiver.exe",
    ):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    raise RuntimeError(f"aether_hot_tcp_receiver.exe not found under {rx_build}")


def _verify_rx_exe(exe: Path, log: list[str]) -> None:
    size = exe.stat().st_size
    line = f"RX_EXE_SIZE={size}"
    print(f"RX_EXE={exe}", flush=True)
    print(line, flush=True)
    log.extend([f"RX_EXE={exe}", line])
    hr = subprocess.run([str(exe), "--help"], capture_output=True, text=True, check=False, timeout=30)
    if hr.returncode not in (0, 1):
        raise RuntimeError(f"receiver --help failed rc={hr.returncode}\n{hr.stderr}")


def _optional_local_aether_cpm_arg(env: dict[str, str]) -> list[str]:
    if not PINNED_AETHER_LOCAL.is_dir():
        return []
    gr = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(PINNED_AETHER_LOCAL),
        capture_output=True,
        text=True,
        check=False,
    )
    head = (gr.stdout or "").strip()
    if head != AETHER_PIN_SHA:
        chk = subprocess.run(
            ["git", "cat-file", "-e", f"{AETHER_PIN_SHA}^{{commit}}"],
            cwd=str(PINNED_AETHER_LOCAL),
            capture_output=True,
            check=False,
        )
        if chk.returncode != 0:
            return []
    print(f"CPM_aether-client-cpp_SOURCE={PINNED_AETHER_LOCAL}", flush=True)
    return [f"-DCPM_aether-client-cpp_SOURCE={PINNED_AETHER_LOCAL.as_posix()}"]


def build_receiver() -> Path:
    global RX_EXE
    RESULTS.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    vs_root = _find_vs_installation()
    env = _capture_msvc_env(vs_root)
    _log_msvc_toolchain(env, log)

    user_cfg = (RX_SRC / "user_config.h").resolve()
    RX_BUILD.mkdir(parents=True, exist_ok=True)

    def run_cmake_configure(use_ninja: bool) -> subprocess.CompletedProcess[str]:
        cmd = [
            "cmake",
            "-S",
            str(RX_SRC),
            "-B",
            str(RX_BUILD),
            f"-DUSER_CONFIG={user_cfg.as_posix()}",
        ]
        if use_ninja:
            cmd.extend(["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"])
            cl = subprocess.run(["where", "cl"], env=env, capture_output=True, text=True, check=False)
            cl_paths = (cl.stdout or "").strip().splitlines()
            if not cl_paths:
                raise RuntimeError("cl.exe not found in MSVC environment")
            cl_full = cl_paths[0]
            cmd.extend(["-DCMAKE_C_COMPILER=" + cl_full, "-DCMAKE_CXX_COMPILER=" + cl_full])
        else:
            cmd.extend(["-G", "Visual Studio 17 2022", "-A", "x64"])
        cmd.extend(_optional_local_aether_cpm_arg(env))
        log.append("CONFIGURE_CMD=" + " ".join(cmd))
        return subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT, check=False)

    gen = "Ninja"
    cr = run_cmake_configure(use_ninja=True)
    log.append(cr.stdout or "")
    log.append(cr.stderr or "")
    if cr.returncode != 0:
        log.append("Ninja+MSVC configure failed; trying Visual Studio generator")
        if RX_BUILD.exists():
            import shutil

            shutil.rmtree(RX_BUILD, ignore_errors=True)
        RX_BUILD.mkdir(parents=True, exist_ok=True)
        gen = "Visual Studio 17 2022"
        cr = run_cmake_configure(use_ninja=False)
        log.append(cr.stdout or "")
        log.append(cr.stderr or "")
        if cr.returncode != 0:
            RX_BUILD_LOG.write_text("\n".join(log), encoding="utf-8")
            raise RuntimeError(f"receiver cmake failed:\n{cr.stdout}\n{cr.stderr}")

    _assert_msvc_configure(RX_BUILD, log)
    print(f"RECEIVER_GENERATOR={gen}", flush=True)
    log.append(f"RECEIVER_GENERATOR={gen}")

    if gen == "Ninja":
        br = subprocess.run(
            ["cmake", "--build", str(RX_BUILD), "--target", "aether_hot_tcp_receiver", "--parallel"],
            env=env,
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
    else:
        br = subprocess.run(
            [
                "cmake",
                "--build",
                str(RX_BUILD),
                "--config",
                "Release",
                "--target",
                "aether_hot_tcp_receiver",
                "--parallel",
            ],
            env=env,
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
    log.append(br.stdout or "")
    log.append(br.stderr or "")
    if br.returncode != 0:
        RX_BUILD_LOG.write_text("\n".join(log), encoding="utf-8")
        raise RuntimeError(f"receiver build failed:\n{br.stdout}\n{br.stderr}")

    RX_EXE = _resolve_rx_exe(RX_BUILD)
    _verify_rx_exe(RX_EXE, log)
    print("RX_BUILD=PASS", flush=True)
    log.append("RX_BUILD=PASS")
    log.append("SmallFunction_patched=no")
    RX_BUILD_LOG.write_text("\n".join(log), encoding="utf-8")
    return RX_EXE


def rx_exe_path() -> Path:
    if RX_EXE and RX_EXE.is_file():
        return RX_EXE
    return _resolve_rx_exe(RX_BUILD)


def _patch_hot_sdkconfig_ulp_off(sdk_path: Path) -> None:
    """Isolate build sdkconfig from repo-root sdkconfig (ULP on for product firmware)."""
    if not sdk_path.is_file():
        return
    lines = sdk_path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    skip_ulp = False
    for line in lines:
        if line.startswith("CONFIG_ULP_COPROC_ENABLED="):
            skip_ulp = True
            out.append("# CONFIG_ULP_COPROC_ENABLED is not set")
            continue
        if skip_ulp and line.startswith("CONFIG_ULP_COPROC_"):
            continue
        skip_ulp = False
        out.append(line)
    text = "\n".join(out) + "\n"
    if "CONFIG_ULP_COPROC_ENABLED" not in text:
        text += "# CONFIG_ULP_COPROC_ENABLED is not set\n"
    sdk_path.write_text(text, encoding="utf-8")


def build_firmware(service_uid: str, *, hot_attempts: int = MEASURED, period_ms: int = 2000) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, password = wifi_creds()
    b = BUILD.as_posix()
    sdk_cfg = (BUILD / "sdkconfig").as_posix()
    defs = (
        f"-D SDKCONFIG='{sdk_cfg}' "
        f"-D SDKCONFIG_DEFAULTS='sdkconfig.defaults.aether_hot_100x_2s_ppk' "
        f"-D AETHER_DIAG_AETHER_HOT_100X_2S_PPK=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-D SERVICE_UID={service_uid} "
        f"-D AE_POWER_BENCH_VARIANT=400 "
        f"-D AE_POWER_BENCH_ARM_MS=20000 "
        f"-D AETHER_POWER_BENCH_HOT_ATTEMPTS={hot_attempts} "
        f"-D AETHER_POWER_BENCH_HOT_SLEEP_MS={period_ms} "
        f"-D AETHER_PREPARED_HOT_RESERVE=128 "
        f"-D BENCH_CLIENT_ID=aether_hot_tcp_bench_v1 "
        f"-B '{b}'"
    )
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} reconfigure
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"esp set-target/reconfigure failed code={r.returncode}")
    _patch_hot_sdkconfig_ulp_off(BUILD / "sdkconfig")
    build_ps = f"""
idf.py {defs} reconfigure
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
idf.py {defs} build
"""
    r = sb.run(sb.idf_cmd(build_ps), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"esp build failed code={r.returncode}")


class TcpReceiverProc:
    def __init__(self, state_dir: Path, tsv_path: Path, expected: int) -> None:
        self.state_dir = state_dir
        self.tsv_path = tsv_path
        self.expected = expected
        self.proc: subprocess.Popen[str] | None = None
        self.lines: deque[str] = deque(maxlen=50_000)
        self.lock = threading.Lock()
        self.receiver_uid = ""
        self.tcp_verified = False
        self.fail_tcp = False
        self.events: list[dict] = []
        self.full_ready_runs: set[int] = set()

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            with self.lock:
                self.lines.append(line)
            print(f"RX|{line}", flush=True)
            m = RE_RX_UID.search(line)
            if m:
                self.receiver_uid = m.group(1)
            if RE_TCP_OK.search(line):
                self.tcp_verified = True
            if RE_FAIL_TCP.search(line):
                self.fail_tcp = True
            m = RE_FULL_READY.search(line)
            if m:
                self.full_ready_runs.add(int(m.group(1)))
            m = RE_HOT_RX.search(line)
            if m:
                run_id, seq, dup, ooo = map(int, m.groups())
                self.events.append(
                    {
                        "t": time.time(),
                        "run_id": run_id,
                        "seq": seq,
                        "duplicate": dup,
                        "out_of_order": ooo,
                    }
                )

    def start(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["AE_RECEIVER_SESSION_DIR"] = str(self.state_dir.resolve())
        cmd = [
            str(rx_exe_path()),
            "--state-dir",
            str(self.state_dir.resolve()),
            "--require-tcp",
            "--expected",
            str(self.expected),
            "--tsv",
            str(self.tsv_path.resolve()),
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.state_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def wait_ready(self, timeout_s: float = 120.0) -> None:
        t0 = time.time()
        tcp_only_since: float | None = None
        while time.time() - t0 < timeout_s:
            if self.fail_tcp:
                raise RuntimeError("FAIL_TCP_RECEIVER during startup")
            with self.lock:
                tail = "\n".join(list(self.lines)[-40:])
            if "RX_TRANSPORT=UDP endpoint" in tail:
                tcp_only_since = None
            elif "RX_TCP_VERIFIED=yes" in tail or "RX_TRANSPORT=TCP" in tail:
                if tcp_only_since is None:
                    tcp_only_since = time.time()
                elif time.time() - tcp_only_since >= 2.0:
                    self.tcp_verified = True
            if self.receiver_uid and self.tcp_verified:
                return
            time.sleep(0.2)
        raise RuntimeError("receiver TCP / UID not ready in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def rx_map(self, run_id: int) -> dict[int, dict]:
        by: dict[int, list] = {}
        for e in self.events:
            if e["run_id"] != run_id:
                continue
            seq = e["seq"]
            if seq < 1 or seq > self.expected:
                continue
            by.setdefault(seq, []).append(e)
        out: dict[int, dict] = {}
        for seq, evs in by.items():
            first = evs[0]
            out[seq] = {
                "received": True,
                "duplicate_count": max(0, len(evs) - 1),
                "t_wall": first["t"],
                "out_of_order": any(x["out_of_order"] for x in evs),
            }
        return out


class ContinuousPpkCapture:
    def __init__(self, ppk: sb.PpkSession, raw_bin: Path, meta_path: Path) -> None:
        self.ppk = ppk
        self.raw_bin = raw_bin
        self.meta_path = meta_path
        self.bin_f = raw_bin.open("wb")
        self.wall_start = 0.0
        self.sample_count = 0
        self.cfg = SegmenterConfig(fs_hz=FS_HZ, cold_boot_ignore_s=COLD_BOOT_S, max_measured=MEASURED)
        self.segmenter = StreamingCycleSegmenter(self.cfg)

    def start(self) -> None:
        self.wall_start = time.time()
        self.ppk.set_on()
        self.ppk.ppk.start_measuring()

    def stop(self) -> None:
        self.segmenter.finalize()
        self.ppk.ppk.stop_measuring()
        self.bin_f.close()
        meta = {
            "fs_hz": FS_HZ,
            "wall_start_utc": datetime.fromtimestamp(self.wall_start, tz=timezone.utc).isoformat(),
            "sample_count": self.sample_count,
            "voltage_mV": VOLTAGE_MV,
            "raw_bin": str(self.raw_bin),
        }
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def drain(self) -> None:
        raw = self.ppk.ppk.get_data()
        if not raw:
            return
        chunk, _ = self.ppk.ppk.get_samples(raw)
        if not chunk:
            return
        arr = np.asarray(chunk, dtype=np.float32)
        arr.tofile(self.bin_f)
        self.sample_count += len(arr)
        self.segmenter.feed_chunk(arr)


def run_ppk_capture(
    ppk: sb.PpkSession, label: str, raw_bin: Path, *, hot_n: int = MEASURED
) -> StreamingCycleSegmenter:
    meta = raw_bin.with_suffix(".json")
    cap = ContinuousPpkCapture(ppk, raw_bin, meta)
    cap.segmenter = StreamingCycleSegmenter(
        SegmenterConfig(fs_hz=FS_HZ, cold_boot_ignore_s=COLD_BOOT_S, max_measured=hot_n)
    )
    cap.start()
    t0 = time.time()
    deadline = t0 + COLD_BOOT_S + hot_n * 6 + 180
    while time.time() < deadline:
        cap.drain()
        if cap.segmenter.measured_windows >= hot_n:
            time.sleep(2.0)
            cap.drain()
            break
        time.sleep(0.002)
    cap.stop()
    print(
        f"CAPTURE {label} measured={cap.segmenter.measured_windows} samples={cap.sample_count}",
        flush=True,
    )
    return cap.segmenter


def build_aether_rows(
    windows: list[CycleWindow], raw_bin: Path, rx: dict[int, dict], *, hot_n: int = MEASURED
) -> list[dict]:
    ordered = sorted(windows, key=lambda w: w.wake_start_s)
    if ordered and ordered[0].sequence == 0:
        measured = ordered[1 : hot_n + 1]
    else:
        measured = ordered[:hot_n]
    rows: list[dict] = []
    prev_wake: float | None = None
    prev_end: float | None = None
    for seq_i, w in enumerate(measured, start=1):
        base = integrate_window(raw_bin, w)
        base["sequence"] = seq_i
        rxinfo = rx.get(seq_i, {"received": False, "duplicate_count": 0, "out_of_order": False})
        sts = w.wake_start_s - prev_wake if prev_wake is not None else None
        sleep_d = w.wake_start_s - prev_end if prev_end is not None else None
        rows.append(
            {
                **base,
                "start_to_start_s": sts,
                "sleep_duration_s": sleep_d,
                "receiver_received": bool(rxinfo.get("received")),
                "duplicate_count": int(rxinfo.get("duplicate_count", 0)),
                "out_of_order": bool(rxinfo.get("out_of_order")),
            }
        )
        prev_wake = w.wake_start_s
        prev_end = w.wake_end_s
    return rows


def write_csv(rows: list[dict], run_id: int) -> None:
    fields = [
        "run_id",
        "sequence",
        "duration_ms",
        "charge_mC",
        "energy_mJ",
        "mean_current_mA",
        "peak_current_mA",
        "start_to_start_s",
        "sleep_duration_s",
        "receiver_received",
        "duplicate_count",
        "out_of_order",
        "valid",
        "invalid_reason",
        "raw_window_path",
    ]
    write_header = not CSV_OUT.exists() or CSV_OUT.stat().st_size == 0
    with CSV_OUT.open("a", encoding="utf-8", newline="") as f:
        if write_header:
            f.write(",".join(fields) + "\n")
        for r in rows:
            f.write(
                ",".join(
                    str(r.get(k, "")) if k != "run_id" else str(run_id)
                    for k in fields
                )
                + "\n"
            )


def receiver_only_smoke(rx: TcpReceiverProc, *, hold_s: float = 15.0) -> int:
    """Receiver TCP + UID without PPK or ESP flash."""
    print("RECEIVER_ONLY_SMOKE=1 PPK_TOUCHED=no ESP_FLASHED=no", flush=True)
    t0 = time.time()
    while time.time() - t0 < hold_s:
        if rx.fail_tcp:
            rx.stop()
            return 2
        if rx.tcp_verified and rx.receiver_uid:
            time.sleep(0.5)
        else:
            time.sleep(0.2)
    if not (rx.receiver_uid and rx.tcp_verified and not rx.fail_tcp):
        rx.stop()
        return 2
    print(f"RECEIVER_UID={rx.receiver_uid}", flush=True)
    print(f"RX_TRANSPORT=TCP RX_TCP_VERIFIED=yes", flush=True)
    rep = {
        "phase": "receiver_only",
        "receiver_uid": rx.receiver_uid,
        "tcp_verified": rx.tcp_verified,
        "ppk_touched": False,
        "esp_flashed": False,
    }
    smoke_json = RESULTS / "aether_hot_receiver_only_smoke.json"
    smoke_json.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    rx.stop()
    return 0


def analyze_run(
    rx: TcpReceiverProc,
    raw_bin: Path,
    *,
    hot_n: int,
    label: str,
) -> tuple[dict, int]:
    measured, windows = resegment_offline(raw_bin)
    run_ids = sorted({e["run_id"] for e in rx.events}) or [0]
    run_id = run_ids[-1] if run_ids else 0
    rx_map = rx.rx_map(run_id)
    rows = build_aether_rows(windows, raw_bin, rx_map, hot_n=hot_n)
    if label == "full":
        write_csv(rows, run_id)
    d_ms = [r["duration_ms"] for r in rows if r.get("valid")]
    d_mc = [r["charge_mC"] for r in rows if r.get("valid")]
    hot_rx = sum(1 for r in rows if r.get("receiver_received"))
    missing = [i for i in range(1, hot_n + 1) if i not in rx_map]
    rep = {
        "label": label,
        "branch": START_BRANCH,
        "sha": START_SHA,
        "receiver_uid": rx.receiver_uid,
        "tcp_verified": rx.tcp_verified,
        "full_ready": bool(rx.full_ready_runs),
        "run_id": run_id,
        "hot_attempts": hot_n,
        "hot_rx": hot_rx,
        "missing_sequences": missing,
        "measured_windows_detected": measured,
        "valid_windows": sum(1 for r in rows if r.get("valid")),
        "stats": {
            "duration_ms": agg_stats(d_ms),
            "charge_mC": agg_stats(d_mc),
        },
        "raw_ppk_local": str(raw_bin.parent),
    }
    ok = (
        rx.tcp_verified
        and not rx.fail_tcp
        and bool(rx.full_ready_runs)
        and measured >= hot_n
        and rep["valid_windows"] >= max(1, hot_n - 1)
    )
    return rep, 0 if ok else 1


def main() -> int:
    global START_BRANCH, START_SHA
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true", help="Skip receiver and firmware build")
    ap.add_argument("--skip-receiver-build", action="store_true")
    ap.add_argument("--skip-firmware-build", action="store_true")
    ap.add_argument("--smoke-only", action="store_true")
    ap.add_argument("--skip-ppk", action="store_true")
    args = ap.parse_args()
    if args.skip_build:
        args.skip_receiver_build = True
        args.skip_firmware_build = True

    START_BRANCH, START_SHA = git_meta()
    hot_n = 5 if args.smoke_only else MEASURED
    print(f"START_BRANCH={START_BRANCH} START_SHA={START_SHA}", flush=True)

    RESULTS.mkdir(parents=True, exist_ok=True)
    LOCAL_RAW.mkdir(parents=True, exist_ok=True)
    WINDOWS_DIR.mkdir(parents=True, exist_ok=True)

    receiver_only = args.smoke_only and args.skip_ppk

    if not args.skip_receiver_build:
        print("=== BUILD RECEIVER (MSVC) ===", flush=True)
        build_receiver()
    elif not rx_exe_path().is_file():
        print("=== BUILD RECEIVER (MSVC) ===", flush=True)
        build_receiver()

    print("=== START RECEIVER (TCP) ===", flush=True)
    tsv = RESULTS / ("aether_hot_tcp_receiver_smoke.tsv" if args.smoke_only else "aether_hot_tcp_receiver_run1.tsv")
    rx = TcpReceiverProc(RX_STATE, tsv, hot_n)
    rx.start()
    try:
        rx.wait_ready(timeout_s=180.0)
    except RuntimeError:
        rx.stop()
        return 2
    print(f"RECEIVER_UID={rx.receiver_uid}", flush=True)

    if receiver_only:
        if not args.skip_firmware_build:
            print("=== BUILD ESP (smoke firmware, no flash) ===", flush=True)
            build_firmware(rx.receiver_uid, hot_attempts=hot_n)
        return receiver_only_smoke(rx)

    if not args.skip_firmware_build or not (BUILD / "flash_args").exists():
        print("=== BUILD ESP ===", flush=True)
        build_firmware(rx.receiver_uid, hot_attempts=hot_n)

    if args.skip_ppk:
        rx.stop()
        return 0

    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    try:
        print("=== FLASH ===", flush=True)
        sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
        time.sleep(1.0)
        port = sb.wait_espressif_com(timeout_s=25.0)
        if port:
            sb.hard_reset(port)
        ppk.power_cycle()
        port = sb.wait_espressif_com(timeout_s=50.0)
        if port:
            sb.hard_reset(port)
        # Match firmware kColdBootFlashWindowS before FULL prepare on cold reset.
        time.sleep(65.0)

        raw_bin = LOCAL_RAW / ("smoke.bin" if args.smoke_only else "run1.bin")
        run_ppk_capture(ppk, "PPK_RUN1", raw_bin, hot_n=hot_n)
        label = "smoke" if args.smoke_only else "full"
        rep, rc = analyze_run(rx, raw_bin, hot_n=hot_n, label=label)
        out_json = (
            RESULTS / "aether_hot_smoke_5_ppk_tcp_rx.json"
            if args.smoke_only
            else JSON_OUT
        )
        out_json.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        MD_OUT.write_text(
            f"# Æther HOT 100x / 2s / PPK / TCP receiver\n\n"
            f"- receiver_uid: `{rx.receiver_uid}`\n"
            f"- TCP verified: {rx.tcp_verified}\n"
            f"- PPK measured windows: {rep['measured_windows_detected']}\n"
            f"- E2E HOT RX: {rep['hot_rx']}/{hot_n}\n"
            f"- json: `{out_json}`\n",
            encoding="utf-8",
        )
        print(json.dumps(rep, indent=2), flush=True)
        rx.stop()
        return rc
    finally:
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
