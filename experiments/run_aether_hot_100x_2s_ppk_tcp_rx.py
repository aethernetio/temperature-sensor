#!/usr/bin/env python3
"""Æther prepared HOT 100x / 2 s / PPK full capture + C++ TCP receiver E2E."""
from __future__ import annotations

import argparse
import hashlib
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
from ppk_stream_cycle_segmenter import (  # noqa: E402
    CloseReason,
    CycleWindow,
    SegmenterConfig,
    StreamingCycleSegmenter,
    aether_hot_segmenter_config,
)
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
FS_INIT_HEADER = HERE / "preprovision" / "sender_fs_157aadbe.h"
BENCH_CLIENT_ID = "reliability_full_v1"

START_BRANCH = ""
START_SHA = ""
RE_RX_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_HOT_RX = re.compile(
    r"AETHER_HOT_RX wall_ms=\d+ run_id=(\d+) seq=(\d+) duplicate=(\d+) out_of_order=(\d+)"
)
RE_FULL_READY = re.compile(r"AETHER_FULL_READY run_id=(\d+)")
RE_TCP_OK = re.compile(r"RX_TCP_VERIFIED=yes")
RE_FAIL_TCP = re.compile(r"FAIL_TCP_RECEIVER=")
RE_PREPARED_SID = re.compile(r"PREPARED_TARGET_SID=([^\s]+)")
RE_RECEIVER_TCP_SIDS = re.compile(r"RECEIVER_TCP_SIDS=([^\s]+)")
RE_HOT_ARMED = re.compile(r"HOT_MEASURE_ARMED run_id=(\d+)")
RE_FULL_READY_TX = re.compile(r"FULL_READY_WRITE_SUCCESS run_id=(\d+)")
RE_PREP_EXPORT = re.compile(r"PREPARED_EXPORT_SUCCESS reserve=(\d+) remaining=(\d+)")

AETHER_PIN_SHA = "157aadbec8e7b852d0f89274307ff7cb8103e5f7"
PINNED_AETHER_LOCAL = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")


def _load_local_wifi_env_file() -> None:
    path = HERE / "local_aether_wifi.env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


def wifi_creds() -> tuple[str, str]:
    _load_local_wifi_env_file()
    ssid = os.environ.get("AETHER_WIFI_SSID", "").strip()
    password = os.environ.get("AETHER_WIFI_PASSWORD", "").strip()
    if not ssid or not password:
        raise RuntimeError(
            "Set AETHER_WIFI_SSID and AETHER_WIFI_PASSWORD in the environment, "
            "or provide experiments/local_aether_wifi.env (gitignored)."
        )
    return ssid, password


def redact_secrets(text: str) -> str:
    pwd = os.environ.get("AETHER_WIFI_PASSWORD", "")
    if pwd:
        text = text.replace(pwd, "***REDACTED***")
    return text


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


def firmware_bin_meta() -> dict:
    bin_path = BUILD / "temperature_sensor.bin"
    out: dict = {"path": str(bin_path), "exists": bin_path.is_file()}
    if bin_path.is_file():
        data = bin_path.read_bytes()
        out["size_bytes"] = len(data)
        out["sha256"] = hashlib.sha256(data).hexdigest()
    _, sha = git_meta()
    out["git_sha"] = sha
    return out


def parse_serial_diagnostics(lines: list[str]) -> dict:
    blob = "\n".join(lines)
    full_w = RE_FULL_READY_TX.search(blob)
    prep = RE_PREP_EXPORT.search(blob)
    hot_arm = RE_HOT_ARMED.search(blob)
    sid = RE_PREPARED_SID.search(blob)
    order_ok = False
    if full_w and prep:
        order_ok = blob.find(full_w.group(0)) < blob.find(prep.group(0))
    return {
        "full_ready_write_seen": bool(full_w),
        "prepared_export_seen": bool(prep),
        "prepared_export_order_ok": order_ok,
        "prepared_reserve": int(prep.group(1)) if prep else None,
        "prepared_remaining": int(prep.group(2)) if prep else None,
        "prepared_target_sid": sid.group(1) if sid else "",
        "hot_measure_armed_seen": bool(hot_arm),
    }


def target_tcp_match_from_rx(rx: TcpReceiverProc, prepared_sid: str) -> tuple[bool, list[str]]:
    with rx.lock:
        tail = "\n".join(list(rx.lines)[-80:])
    m = RE_RECEIVER_TCP_SIDS.search(tail)
    sids = m.group(1).split(",") if m else []
    if not prepared_sid or not sids:
        return False, sids
    return prepared_sid.strip() in [s.strip() for s in sids], sids


def erase_flash_chip(port: str) -> None:
    cmd = [
        str(sb.IDF_PYTHON),
        "-m",
        "esptool",
        "--chip",
        "esp32c6",
        "-p",
        port,
        "-b",
        "460800",
        "erase_flash",
    ]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(cmd, cwd=str(BUILD), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"erase_flash failed: {r.stderr or r.stdout}")


def build_firmware(service_uid: str, *, hot_attempts: int = MEASURED, period_ms: int = 2000) -> None:
    if not FS_INIT_HEADER.is_file():
        raise RuntimeError(f"missing FS_INIT header: {FS_INIT_HEADER}")
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, _password = wifi_creds()
    b = BUILD.as_posix()
    sdk_cfg = (BUILD / "sdkconfig").as_posix()
    defs = (
        f"-D SDKCONFIG='{sdk_cfg}' "
        f"-D SDKCONFIG_DEFAULTS='sdkconfig.defaults.aether_hot_100x_2s_ppk' "
        f"-D AETHER_DIAG_AETHER_HOT_100X_2S_PPK=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D SERVICE_UID={service_uid} "
        f"-D AE_POWER_BENCH_VARIANT=400 "
        f"-D AE_POWER_BENCH_ARM_MS=20000 "
        f"-D AETHER_POWER_BENCH_HOT_ATTEMPTS={hot_attempts} "
        f"-D AETHER_POWER_BENCH_HOT_SLEEP_MS={period_ms} "
        f"-D AETHER_PREPARED_HOT_RESERVE=128 "
        f"-D BENCH_CLIENT_ID={BENCH_CLIENT_ID} "
        f"-D FS_INIT={FS_INIT_HEADER.as_posix()} "
    )
    if hot_attempts <= 5:
        defs += " -D AE_EXP_BISECT_CONSOLE=1 "
    defs += f"-B '{b}'"
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} reconfigure
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    idf_env = os.environ.copy()
    wifi_creds()  # validate env present; password only via AETHER_WIFI_PASSWORD for CMake
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT), env=idf_env)
    if r.returncode != 0:
        raise RuntimeError(
            redact_secrets(f"esp set-target/reconfigure failed code={r.returncode}")
        )
    _patch_hot_sdkconfig_ulp_off(BUILD / "sdkconfig")
    build_ps = f"""
idf.py {defs} reconfigure
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
idf.py {defs} build
"""
    r = sb.run(sb.idf_cmd(build_ps), cwd=str(ROOT), env=idf_env)
    if r.returncode != 0:
        raise RuntimeError(redact_secrets(f"esp build failed code={r.returncode}"))


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
    def __init__(
        self,
        ppk: sb.PpkSession,
        raw_bin: Path,
        meta_path: Path,
        *,
        hot_n: int,
        measured_start_s: float | None = None,
    ) -> None:
        self.ppk = ppk
        self.raw_bin = raw_bin
        self.meta_path = meta_path
        self.bin_f = raw_bin.open("wb")
        self.wall_start = 0.0
        self.power_on_wall = 0.0
        self.cfg = aether_hot_segmenter_config(hot_n=hot_n, measured_start_s=measured_start_s)
        self.segmenter = StreamingCycleSegmenter(self.cfg)
        self.sample_count = 0

    def start(self) -> None:
        self.wall_start = time.time()
        self.ppk.ppk.start_measuring()

    def stop(self) -> None:
        self.segmenter.finalize()
        self.ppk.ppk.stop_measuring()
        self.bin_f.close()
        meta = {
            "fs_hz": FS_HZ,
            "wall_start_utc": datetime.fromtimestamp(self.wall_start, tz=timezone.utc).isoformat(),
            "power_on_wall_utc": datetime.fromtimestamp(self.power_on_wall, tz=timezone.utc).isoformat(),
            "measured_start_s": self.cfg.measured_start_s,
            "max_wake_s": self.cfg.max_wake_s,
            "sample_count": self.sample_count,
            "force_closed_windows": self.segmenter.force_closed_count,
            "clipped_windows": self.segmenter.clipped_windows,
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


def resegment_aether_offline(
    raw_bin: Path, *, hot_n: int, measured_start_s: float | None
) -> tuple[int, list[CycleWindow], StreamingCycleSegmenter]:
    u = np.fromfile(raw_bin, dtype=np.float32)
    seg = StreamingCycleSegmenter(
        aether_hot_segmenter_config(hot_n=hot_n, measured_start_s=measured_start_s)
    )
    chunk = 100_000
    for i in range(0, len(u), chunk):
        seg.feed_chunk(u[i : i + chunk])
    seg.finalize()
    measured = len([w for w in seg.windows_closed if 1 <= w.sequence <= hot_n])
    return measured, seg.windows_closed, seg


def run_ppk_capture(
    ppk: sb.PpkSession,
    label: str,
    raw_bin: Path,
    *,
    hot_n: int = MEASURED,
    measured_start_s: float | None = None,
    trailing_sleep_s: float = 30.0,
) -> StreamingCycleSegmenter:
    meta = raw_bin.with_suffix(".json")
    cap = ContinuousPpkCapture(
        ppk, raw_bin, meta, hot_n=hot_n, measured_start_s=measured_start_s
    )
    cap.start()
    cap.power_on_wall = time.time()
    ppk.power_cycle()
    t0 = time.time()
    max_wake = cap.cfg.max_wake_s
    deadline = t0 + COLD_BOOT_S + 120.0 + hot_n * (max_wake + PERIOD_S) + trailing_sleep_s
    while time.time() < deadline:
        cap.drain()
        if cap.segmenter.measured_windows >= hot_n:
            time.sleep(trailing_sleep_s)
            cap.drain()
            break
        time.sleep(0.002)
    cap.stop()
    print(
        f"CAPTURE {label} measured={cap.segmenter.measured_windows} "
        f"clipped={cap.segmenter.clipped_windows} force={cap.segmenter.force_closed_count} "
        f"samples={cap.sample_count}",
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


def delivery_report(rx: TcpReceiverProc, hot_n: int) -> dict:
    run_ids = sorted({e["run_id"] for e in rx.events}) or [0]
    run_id = run_ids[-1]
    rx_map = rx.rx_map(run_id)
    missing = [i for i in range(1, hot_n + 1) if i not in rx_map]
    dups = sum(max(0, len({e["seq"] for e in rx.events if e["run_id"] == run_id and e["seq"] == s}) - 1) for s in range(1, hot_n + 1))
    ooo = sum(1 for e in rx.events if e["run_id"] == run_id and e.get("out_of_order"))
    return {
        "run_id": run_id,
        "full_ready": run_id in rx.full_ready_runs,
        "hot_rx": len(rx_map),
        "missing_sequences": missing,
        "duplicates": dups,
        "out_of_order": ooo,
    }


def wait_delivery_run(ppk: sb.PpkSession, rx: TcpReceiverProc, hot_n: int) -> None:
    rx.events.clear()
    rx.full_ready_runs.clear()
    ppk.power_cycle()
    deadline = time.time() + COLD_BOOT_S + 180.0 + hot_n * (PERIOD_S + 45.0)
    while time.time() < deadline:
        run_ids = {e["run_id"] for e in rx.events}
        if not run_ids:
            time.sleep(0.2)
            continue
        run_id = max(run_ids)
        if run_id not in rx.full_ready_runs:
            time.sleep(0.2)
            continue
        rx_map = rx.rx_map(run_id)
        if len(rx_map) >= hot_n:
            time.sleep(15.0)
            return
        time.sleep(0.2)


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
    measured_start_s: float | None = None,
    seg: StreamingCycleSegmenter | None = None,
) -> tuple[dict, int]:
    if seg is None:
        measured, windows, seg = resegment_aether_offline(
            raw_bin, hot_n=hot_n, measured_start_s=measured_start_s
        )
    else:
        windows = seg.windows_closed
        measured = seg.measured_windows
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
    clipped = seg.clipped_windows if seg else sum(
        1 for w in windows if 1 <= w.sequence <= hot_n and w.close_reason == CloseReason.FORCE_LIMIT
    )
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
        "clipped_windows": clipped,
        "force_closed_windows": seg.force_closed_count if seg else 0,
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
        and clipped == 0
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
    serial_log_path = RESULTS / "aether_hot_smoke_serial.log"
    serial_lines: list[str] = []
    serial_stop = threading.Event()

    def serial_reader() -> None:
        port: str | None = None
        for _ in range(90):
            if serial_stop.is_set():
                return
            port = sb.wait_espressif_com(timeout_s=2.0)
            if port:
                break
            time.sleep(0.5)
        if not port:
            serial_lines.append("SERIAL_ERR=no_espressif_com")
            return
        for attempt in range(8):
            if serial_stop.is_set():
                return
            try:
                import serial  # type: ignore

                with serial.Serial(port, 115200, timeout=0.2) as ser:
                    serial_lines.append(f"SERIAL_OPEN port={port}")
                    while not serial_stop.is_set():
                        raw = ser.readline()
                        if not raw:
                            continue
                        line = raw.decode("utf-8", errors="replace").rstrip()
                        serial_lines.append(line)
                        print(f"ESP|{redact_secrets(line)}", flush=True)
                return
            except Exception as exc:
                serial_lines.append(f"SERIAL_ERR={exc!r}")
                time.sleep(1.0)
                port = sb.wait_espressif_com(timeout_s=5.0) or port

    serial_thread: threading.Thread | None = None

    try:
        print("=== ERASE + FLASH (not measuring) ===", flush=True)
        ppk.power_cycle()
        erase_port = sb.wait_espressif_com(timeout_s=45.0)
        if erase_port:
            print(f"ERASE_FLASH port={erase_port}", flush=True)
            erase_flash_chip(erase_port)
        sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
        fw_meta = firmware_bin_meta()
        print(f"FIRMWARE_BIN_SHA256={fw_meta.get('sha256', '')}", flush=True)
        ppk.ppk.stop_measuring()
        ppk.set_off()
        time.sleep(0.5)
        if args.smoke_only:
            time.sleep(2.0)
            serial_thread = threading.Thread(target=serial_reader, daemon=True)
            serial_thread.start()

        def one_run(
            raw_bin: Path,
            label: str,
            *,
            with_ppk: bool,
            wait_after_hot_s: float = 15.0,
        ) -> tuple[StreamingCycleSegmenter | None, float | None]:
            measured_start_s: float | None = None
            seg: StreamingCycleSegmenter | None = None
            if with_ppk:
                meta = raw_bin.with_suffix(".json")
                cap = ContinuousPpkCapture(
                    ppk, raw_bin, meta, hot_n=hot_n, measured_start_s=None
                )
                cap.start()
                cap.power_on_wall = time.time()
                ppk.power_cycle()
            else:
                ppk.power_cycle()
                time.sleep(0.5)
            t_cap = time.time()
            max_wake = 45.0
            full_deadline = t_cap + COLD_BOOT_S + 300.0
            total_deadline = t_cap + COLD_BOOT_S + 300.0 + hot_n * (max_wake + PERIOD_S) + 45.0
            full_seen = False
            while time.time() < total_deadline:
                if with_ppk:
                    cap.drain()
                    seg = cap.segmenter
                if rx.full_ready_runs:
                    full_seen = True
                run_ids = {e["run_id"] for e in rx.events if e["run_id"]}
                hot_count = 0
                if run_ids:
                    rid = max(run_ids)
                    hot_count = len(rx.rx_map(rid))
                if not full_seen and time.time() > full_deadline:
                    break
                if full_seen and hot_count >= hot_n:
                    break
                if with_ppk and full_seen and seg and seg.measured_windows >= hot_n:
                    break
                time.sleep(0.05 if not with_ppk else 0.002)
            time.sleep(wait_after_hot_s)
            if with_ppk:
                cap.drain()
                cap.stop()
                seg = cap.segmenter
            serial_blob = "\n".join(serial_lines)
            m = RE_HOT_ARMED.search(serial_blob)
            if m and with_ppk and cap.wall_start > 0:
                hot_wall = time.time() - wait_after_hot_s
                for line in reversed(serial_lines):
                    if RE_HOT_ARMED.search(line):
                        break
                measured_start_s = max(COLD_BOOT_S, hot_wall - cap.wall_start - 1.0)
            return seg, measured_start_s

        if args.smoke_only:
            raw_bin = LOCAL_RAW / "smoke.bin"
            seg, measured_start_s = one_run(raw_bin, "smoke", with_ppk=True)
            serial_stop.set()
            if serial_thread:
                serial_thread.join(timeout=5.0)
            serial_log_path.write_text(
                redact_secrets("\n".join(serial_lines)), encoding="utf-8"
            )
            label = "smoke"
            rep, rc = analyze_run(
                rx, raw_bin, hot_n=hot_n, label=label, measured_start_s=measured_start_s, seg=seg
            )
            ser = parse_serial_diagnostics(serial_lines)
            rep["firmware"] = fw_meta
            rep["serial"] = ser
            match, tcp_sids = target_tcp_match_from_rx(rx, ser.get("prepared_target_sid", ""))
            rep["receiver_tcp_sids"] = tcp_sids
            rep["TARGET_TCP_MATCH"] = "yes" if match else "no"
            rep["energy_confirmed"] = bool(
                rep.get("full_ready")
                and rep.get("clipped_windows", 1) == 0
                and rep.get("valid_windows", 0) >= hot_n
            )
            rep["smoke_state"] = "PASS" if rc == 0 else "FAIL"
            out_json = RESULTS / "aether_hot_smoke_fixed.json"
        else:
            raw1 = LOCAL_RAW / "run1.bin"
            seg1, ms1 = one_run(raw1, "run1", with_ppk=True)
            rep1, _ = analyze_run(
                rx, raw1, hot_n=hot_n, label="full", measured_start_s=ms1, seg=seg1
            )
            wait_delivery_run(ppk, rx, hot_n)
            rep2 = delivery_report(rx, hot_n)
            wait_delivery_run(ppk, rx, hot_n)
            rep3 = delivery_report(rx, hot_n)
            rep = {
                "run1": rep1,
                "run2": rep2,
                "run3": rep3,
                "branch": START_BRANCH,
                "sha": START_SHA,
                "CREDENTIAL_ROTATION_REQUIRED": "yes",
            }
            out_json = JSON_OUT
            rc = 0 if (
                rep1.get("clipped_windows", 1) == 0
                and rep1.get("valid_windows", 0) >= 95
                and rep1.get("hot_rx", 0) >= hot_n - 3
                and rep2.get("hot_rx", 0) >= hot_n - 3
                and rep3.get("hot_rx", 0) >= hot_n - 3
            ) else 1

        out_json.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        MD_OUT.write_text(
            redact_secrets(
                f"# Æther HOT 100x / 2s / PPK / TCP receiver\n\n"
                f"- json: `{out_json}`\n"
            ),
            encoding="utf-8",
        )
        print(redact_secrets(json.dumps(rep, indent=2)), flush=True)
        rx.stop()
        return rc
    finally:
        serial_stop.set()
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
