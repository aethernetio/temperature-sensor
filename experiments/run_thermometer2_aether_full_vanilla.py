#!/usr/bin/env python3
"""Thermometer 2: full Æther + vanilla Wi-Fi deep-sleep benchmark runner."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
_VENV_PY = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared") / "experiments" / "ppk2-venv" / "Scripts" / "python.exe"
try:
    import numpy as np
except ModuleNotFoundError:
    if _VENV_PY.is_file() and Path(sys.executable).resolve() != _VENV_PY.resolve():
        os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])
    raise
ROOT = HERE.parent
PREPARED_ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
sys.path.insert(0, str(HERE))
if str(PREPARED_ROOT / "experiments") not in sys.path:
    sys.path.append(str(PREPARED_ROOT / "experiments"))
BUILD = ROOT / "build-thermometer2-full-vanilla-v2"
USER_CONFIG_VANILLA = ROOT / "main" / "user_config_full_vanilla.h"
RESULTS = HERE / "thermometer2_full_vanilla_results"
MEASURED = 100
SLEEP_S = 20
VOLTAGE_MV = 3000
VOLTAGE_V = VOLTAGE_MV / 1000.0
FS_HZ = 100_000.0
GUARD_SAMPLES = int(0.75 * FS_HZ)
COLD_BOOT_S = 60.0
RAW_DIR = RESULTS / "ppk_raw"
CSV_OUT = RESULTS / "thermometer2_full_vanilla_100.csv"
JSON_OUT = RESULTS / "thermometer2_full_vanilla_100.json"
MD_OUT = HERE / "FULL_VANILLA.md"

import run_thermometer2_sleep_bisect as sb  # noqa: E402
from ppk_stream_cycle_segmenter import (  # noqa: E402
    CloseReason,
    CycleWindow,
    StreamingCycleSegmenter,
    thermometer2_full_vanilla_segmenter_config,
)
RX_SRC = PREPARED_ROOT / "temperature_receiver"
RX_BUILD = HERE / "thermometer2_full_vanilla_rx_build"
AETHER_LOCAL = ROOT / "deps" / "aether-client-cpp-157aadbe-diag"
# FS_INIT intentionally not used for FULL vanilla (registration + SPIFFS path).
AETHER_PIN = "157aadbec8e7b852d0f89274307ff7cb8103e5f7"
AETHER_PATCH = HERE / "patches" / "aether-157aadbe-full-vanilla-stage1.patch"
BENCH_CLIENT_ID = "reliability_full_v1"
RX_STAMP = RX_BUILD / "receiver_build_stamp.json"
SMOKE_RX_POST_COLLECT_S = 35.0

RE_RX_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_FULL_RX = re.compile(r"AETHER_FULL_READY run_id=(\d+)")
RE_RUN_ID = re.compile(r"run_id=(\d+)")
RE_JOURNAL = re.compile(
    r"JOURNAL cycle=(\d+) run_id=(\d+) got_ip=(\d+) select_ok=(\d+) "
    r"write_called=(\d+) write_status=(\d+) fail_stage=(\d+) "
    r"active_(?:total_)?ms=(\d+)"
)
RE_JOURNAL_DUMP = re.compile(
    r"JOURNAL_DUMP cycle=(\d+) run_id=(\d+) got_ip=(\d+) select_ok=(\d+) "
    r"write_called=(\d+) write_status=(\d+) fail_stage=(\d+) "
    r"active_(?:total_)?ms=(\d+)"
)
RE_BOOT_RUN = re.compile(r"BOOT cycle=(\d+) session=\d+ run_id=(\d+)")
RE_WRITE_TERMINAL = re.compile(r"WRITE_TERMINAL status=(\d+) run_id=(\d+)")
RE_SELECT_OK = re.compile(r"SELECT_CLIENT_RESULT ok=1 uid=([0-9a-f-]{36})")
RE_SENDER_UID_RTC = re.compile(r"SENDER_UID_RTC stored=([0-9a-f-]{36})")
ESPRESSIF_VID = 0x303A
USB_RECOVERY_LOG = RESULTS / "usb_recovery.log"
SMOKE_CYCLES = 3
RUNNER_LOCK = RESULTS / ".stage1_runner.lock"
CONFIRMED_SMOKE_PASS_RUN = RESULTS / "runs" / "smoke_20261017T160227Z"
WRONG_PROFILE_HOT_ARCHIVE = RESULTS / "archive" / "wrong_profile_aether_hot_20261018.json"
RUN_STATE_PATH = RESULTS / "run.json"
PATCH_SHA256_EXPECTED = (
    "7f0c2642638142ed0c5eebc63e3b244ffaff6da5e93f046145617f85a54b888c"
)
RE_BENCH_MODE = re.compile(r"BENCH_MODE=FULL_VANILLA")
RE_BENCH_CYCLES = re.compile(r"cycles=(\d+)")
RE_BENCH_SLEEP = re.compile(r"sleep_s=(\d+)")
RE_BENCH_SVC = re.compile(r"service_uid=([0-9a-f-]{36})", re.I)
RE_BENCH_HOT = re.compile(r"hot_prepared=([01])")
SMOKE_CAPTURE_MAX_S = 1200.0
BOOTSTRAP_MAX_S = 180.0
MEASURE_DEADLINE_S = COLD_BOOT_S + MEASURED * (120.0 + SLEEP_S) + 1500.0


def load_wifi() -> tuple[str, str]:
    for env_path in (
        HERE / "local_aether_wifi.env",
        PREPARED_ROOT / "experiments" / "local_aether_wifi.env",
    ):
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"")
            if k and v and k not in os.environ:
                os.environ[k] = v
        break
    ssid = os.environ.get("AETHER_WIFI_SSID", "").strip()
    pwd = os.environ.get("AETHER_WIFI_PASSWORD", "").strip()
    if not ssid or not pwd:
        raise RuntimeError("Set AETHER_WIFI_SSID/AETHER_WIFI_PASSWORD or local_aether_wifi.env")
    return ssid, pwd


def idf_ps(extra: str) -> list[str]:
    export = Path(r"C:\Espressif\frameworks\esp-idf-v6.0.2\export.ps1")
    ps = f"""
. '{export.as_posix()}'
Set-Location '{ROOT.as_posix()}'
{extra}
exit $LASTEXITCODE
"""
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps,
    ]


def file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path, *, globs: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    if not root.is_dir():
        return ""
    files: list[Path] = []
    for g in globs:
        files.extend(root.glob(g))
    for path in sorted(set(files)):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix().encode("utf-8")
        h.update(rel)
        h.update(path.read_bytes())
    return h.hexdigest()


def ensure_aether_patched() -> dict:
    if not AETHER_PATCH.is_file():
        raise RuntimeError(f"missing aether patch {AETHER_PATCH}")
    patch_sha = file_sha256(AETHER_PATCH)
    if not AETHER_LOCAL.is_dir():
        raise RuntimeError(
            f"missing {AETHER_LOCAL}; clone aether-client-cpp at {AETHER_PIN} then apply patch"
        )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(AETHER_LOCAL), text=True
    ).strip()
    marker = AETHER_LOCAL / "aether" / "server.cpp"
    text = marker.read_text(encoding="utf-8", errors="replace") if marker.is_file() else ""
    aether_cpp = (AETHER_LOCAL / "aether" / "aether.cpp").read_text(
        encoding="utf-8", errors="replace"
    )
    patched = (
        "RebuildChannelsFromAdapters" in text
        and "MakeSelectClientLoadFailed" in aether_cpp
    )
    if head != AETHER_PIN:
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", AETHER_PIN],
            cwd=str(AETHER_LOCAL),
            check=False,
        )
        subprocess.run(
            ["git", "checkout", "--force", AETHER_PIN],
            cwd=str(AETHER_LOCAL),
            check=True,
        )
        patched = False
        head = AETHER_PIN
    if not patched:
        chk = subprocess.run(
            ["git", "apply", "--check", str(AETHER_PATCH)],
            cwd=str(AETHER_LOCAL),
            capture_output=True,
            text=True,
        )
        if chk.returncode != 0:
            raise RuntimeError(
                f"aether patch does not apply cleanly at {AETHER_PIN}: {chk.stderr}"
            )
        subprocess.run(
            ["git", "apply", str(AETHER_PATCH)], cwd=str(AETHER_LOCAL), check=True
        )
    return {
        "aether_pin": AETHER_PIN,
        "aether_head": head,
        "aether_patch_path": str(AETHER_PATCH),
        "aether_patch_sha256": patch_sha,
    }


def receiver_source_fingerprint() -> dict:
    src_tree = tree_sha256(
        RX_SRC,
        globs=("CMakeLists.txt", "src/**/*.cpp", "src/**/*.h", "src/**/*.hpp"),
    )
    ae_tree = tree_sha256(
        AETHER_LOCAL,
        globs=(
            "aether/server.cpp",
            "aether/server.h",
            "aether/adapters/wifi_adapter.*",
            "aether/platform/esp_wifi_driver.cpp",
        ),
    )
    return {
        "receiver_source_root": str(RX_SRC),
        "receiver_source_tree_sha256": src_tree,
        "aether_deps_tree_sha256": ae_tree,
        "aether_pin": AETHER_PIN,
        "aether_patch_sha256": file_sha256(AETHER_PATCH),
    }


def run_idf(extra: str) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(idf_ps(extra), cwd=str(ROOT), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"idf failed code={r.returncode}")


def audit_aether_compile_macros() -> dict:
    cc = BUILD / "compile_commands.json"
    if not cc.is_file():
        return {"compile_commands": "missing"}
    try:
        data = json.loads(cc.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"compile_commands": "invalid_json"}
    needles = (
        "AE_SUPPORT_REGISTRATION",
        "AE_SUPPORT_CLOUD_DNS",
        "AE_SUPPORT_TCP",
        "AE_SUPPORT_UDP",
        "FS_INIT",
    )
    hits: dict[str, list[str]] = {n: [] for n in needles}
    for ent in data:
        cmd = ent.get("command") or ent.get("arguments") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        if "aether-client-cpp" not in cmd and "/aether/" not in cmd.replace("\\", "/"):
            if "thermometer2_aether_full_vanilla_bench.cpp" not in cmd:
                continue
        for n in needles:
            if n in cmd:
                hits[n].append(cmd[:240])
    out: dict[str, str | int] = {
        "user_config": str(USER_CONFIG_VANILLA),
        "fs_init_cmake": "not_set",
        "domain_storage": "AE_SUPPORT_SPIFS_FS (via USER_CONFIG)",
    }
    for n in needles:
        out[f"aether_compile_mentions_{n}"] = len(hits[n])
        if hits[n]:
            out[f"sample_{n}"] = hits[n][0]
    main_hits = [
        e
        for e in data
        if "thermometer2_aether_full_vanilla_bench.cpp" in (e.get("file") or "")
    ]
    if main_hits:
        cmd = main_hits[0].get("command") or ""
        out["main_fs_init_defined"] = 1 if "FS_INIT=" in cmd else 0
        out["main_registration"] = (
            1 if "AE_SUPPORT_REGISTRATION=1" in cmd or '-DAE_SUPPORT_REGISTRATION=1' in cmd else 0
        )
    return out


def build_esp(*, service_uid: str, ssid: str, password: str, smoke: bool) -> None:
    b = BUILD.as_posix()
    pwd = password.replace("'", "''")
    ssid_ps = ssid.replace("'", "''")
    aether = AETHER_LOCAL.as_posix() if AETHER_LOCAL.is_dir() else ""
    ucfg = USER_CONFIG_VANILLA.as_posix()
    cycles = "3" if smoke else "100"
    stage = "1" if smoke else "0"
    sdk_defaults = (
        f"{ROOT.as_posix()}/sdkconfig.defaults;"
        f"{ROOT.as_posix()}/sdkconfig.defaults.thermometer2_aether_full_vanilla"
    )
    if smoke:
        sdk_defaults += f";{ROOT.as_posix()}/sdkconfig.defaults.bench"
    sdkconfig_b = f"{b}/sdkconfig"
    extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd}'
$env:SDKCONFIG = '{sdkconfig_b}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG={sdkconfig_b}',
  '-D','SDKCONFIG_DEFAULTS={sdk_defaults}',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_THERMOMETER2_FULL_VANILLA=1',
  '-D','BOARD=0',
  '-D','AE_EXP_WIFI_CANONICAL=1',
  '-D','AE_EXP_BENCH_STAGE={stage}',
  '-D','AE_THERM_FULL_MEASURE_CYCLES={cycles}',
  '-D','AE_THERM_FULL_VANILLA_SLEEP_S=20',
  '-D','AE_THERM_FULL_ACTIVE_TIMEOUT_MS=60000',
  '-D','CPM_aether-client-cpp_SOURCE={aether}',
  '-D','USER_CONFIG={ucfg}'
)
"""
    extra += f"""
if (Test-Path '{b}/CMakeCache.txt') {{
  $ok = Select-String -Path '{b}/CMakeCache.txt' -Pattern 'user_config_full_vanilla.h' -Quiet
  if (-not $ok) {{
    Write-Host 'USER_CONFIG cache mismatch; removing build dir'
    Remove-Item -LiteralPath '{b}' -Recurse -Force
  }}
}}
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
$env:CMAKE_BUILD_PARALLEL_LEVEL = '1'
$env:NINJA_STATUS = '[%f/%t] '
idf.py @common reconfigure build
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    run_idf(extra)


def build_receiver(*, force: bool = False) -> Path:
    ensure_aether_patched()
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    exe = RX_BUILD / "aether_hot_tcp_receiver.exe"
    staged = RX_BUILD / "aether_hot_tcp_receiver.staged.exe"
    fp = receiver_source_fingerprint()
    want_stamp = {**fp, "cmake_build_dir": str(RX_BUILD)}
    if not force and RX_STAMP.is_file() and exe.is_file():
        try:
            have = json.loads(RX_STAMP.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            have = {}
        if have.get("receiver_source_tree_sha256") == fp["receiver_source_tree_sha256"] and (
            have.get("aether_deps_tree_sha256") == fp["aether_deps_tree_sha256"]
        ):
            have_exe_sha = have.get("exe_sha256") or file_sha256(exe)
            if have_exe_sha == file_sha256(exe):
                return exe
    ps = f"""
$ErrorActionPreference='Stop'
$vswhere = "${{env:ProgramFiles(x86)}}\\Microsoft Visual Studio\\Installer\\vswhere.exe"
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvars = Join-Path $vs 'VC\\Auxiliary\\Build\\vcvars64.bat'
cmd /c "`"$vcvars`" && cmake -S `"{RX_SRC.as_posix()}`" -B `"{RX_BUILD.as_posix()}`" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCPM_aether-client-cpp_SOURCE=`"{AETHER_LOCAL.as_posix()}`" && cmake --build `"{RX_BUILD.as_posix()}`" --target aether_hot_tcp_receiver --parallel"
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError("receiver build failed")
    built = RX_BUILD / "aether_hot_tcp_receiver.exe"
    if not built.is_file():
        alt = RX_BUILD / "Release" / "aether_hot_tcp_receiver.exe"
        if alt.is_file():
            built = alt
        else:
            raise RuntimeError(f"missing receiver exe under {RX_BUILD}")
    staged.write_bytes(built.read_bytes())
    if exe.is_file() and file_sha256(exe) != file_sha256(staged):
        try:
            exe.unlink()
        except OSError:
            pass
    if not exe.is_file():
        staged.replace(exe)
    stamp = {
        **want_stamp,
        "exe_sha256": file_sha256(exe),
        "built_utc": datetime.now(timezone.utc).isoformat(),
    }
    RX_STAMP.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return exe


def start_receiver(exe: Path, session: Path) -> subprocess.Popen:
    session.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["AE_RECEIVER_SESSION_DIR"] = str(session)
    log = RESULTS / "receiver.log"
    RESULTS.mkdir(parents=True, exist_ok=True)
    if log.is_file():
        prev = log.read_text(encoding="utf-8", errors="replace")
        if prev.strip():
            log.with_suffix(".log.prev").write_text(prev, encoding="utf-8")
    return subprocess.Popen(
        [str(exe), "--require-tcp"],
        stdout=open(log, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        env=env,
    )


def ensure_receiver_alive(proc: subprocess.Popen, uid: str, rx_log: Path) -> None:
    if proc.poll() is not None:
        raise RuntimeError(
            f"receiver process exited code={proc.returncode} (infrastructure failure)"
        )
    text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.is_file() else ""
    uids = RE_RX_UID.findall(text)
    if not any(u.lower() == uid.lower() for u in uids):
        raise RuntimeError(
            f"receiver UID missing from log expected={uid} seen={uids[-1] if uids else None}"
        )
    if "RX_TRANSPORT=TCP" not in text and "tcp_up=1" not in text:
        raise RuntimeError("receiver TCP transport not confirmed in log")


def wait_uid(log: Path, timeout: float = 300.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.is_file():
            m = RE_RX_UID.search(log.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
        time.sleep(1.0)
    raise RuntimeError("receiver UID timeout")


def acquire_runner_lock() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    if RUNNER_LOCK.is_file():
        try:
            info = json.loads(RUNNER_LOCK.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            info = {}
        pid = int(info.get("pid") or 0)
        if pid > 0:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
            )
            if str(pid) in (r.stdout or ""):
                raise RuntimeError(
                    f"Stage 1 runner already active pid={pid} started={info.get('started_utc')}"
                )
        RUNNER_LOCK.unlink(missing_ok=True)
    RUNNER_LOCK.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "argv": sys.argv,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def release_runner_lock() -> None:
    if not RUNNER_LOCK.is_file():
        return
    try:
        info = json.loads(RUNNER_LOCK.read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) == os.getpid():
            RUNNER_LOCK.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError):
        RUNNER_LOCK.unlink(missing_ok=True)


def new_campaign_run_dir() -> tuple[Path, str]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = RESULTS / "runs" / f"campaign_{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "kind": "smoke_and_measure",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return d, run_id


def new_smoke_run_dir(parent: Path | None = None) -> Path:
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
        if not (parent / "manifest.json").is_file():
            (parent / "manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": parent.name,
                        "started_utc": datetime.now(timezone.utc).isoformat(),
                        "smoke_cycles": SMOKE_CYCLES,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return parent
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = RESULTS / "runs" / f"smoke_{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "smoke_cycles": SMOKE_CYCLES,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return d


def archive_wrong_profile_hot_run() -> None:
    """Keep mistaken HOT benchmark separate from FULL vanilla Stage 1."""
    src = Path(
        r"C:\Users\nickc\Projects\temperature-sensor-prepared\experiments\power_factor_results\aether_hot_100x_2s_ppk_tcp_rx.json"
    )
    WRONG_PROFILE_HOT_ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    if WRONG_PROFILE_HOT_ARCHIVE.is_file():
        return
    payload = {
        "note": "Wrong profile — not Stage 1 FULL vanilla. Do not merge into vanilla results.",
        "runner": "run_aether_hot_100x_2s_ppk_tcp_rx.py",
        "branch": "diag/aether-hot-e2e-fix-100x-2s",
        "sha": "6de41ff0775f50028b5762a7d34f92930a0ef952",
        "RX": 0,
        "actual_attempts": "UNKNOWN",
        "energy_confirmed": False,
    }
    if src.is_file():
        try:
            hot = json.loads(src.read_text(encoding="utf-8"))
            r1 = hot.get("run1") or {}
            payload["hot_rx_run1"] = r1.get("hot_rx")
            payload["full_ready_run1"] = r1.get("full_ready")
            payload["receiver_uid"] = r1.get("receiver_uid")
            payload["source_json"] = str(src)
        except json.JSONDecodeError:
            payload["source_json"] = str(src)
    WRONG_PROFILE_HOT_ARCHIVE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_run_state(**fields: object) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    state: dict = {}
    if RUN_STATE_PATH.is_file():
        try:
            state = json.loads(RUN_STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    state.setdefault("profile", "FULL_VANILLA_STAGE1")
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    state.update(fields)
    RUN_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _main_bench_compile_command() -> str:
    cc = BUILD / "compile_commands.json"
    if not cc.is_file():
        return ""
    try:
        data = json.loads(cc.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    for ent in data:
        f = (ent.get("file") or "").replace("\\", "/")
        if "thermometer2_aether_full_vanilla_bench.cpp" in f:
            cmd = ent.get("command") or ent.get("arguments") or ""
            if isinstance(cmd, list):
                return " ".join(str(x) for x in cmd)
            return str(cmd)
    return ""


def verify_build_profile(*, service_uid: str, smoke: bool) -> dict:
    """Pre-flash audit: refuse HOT/prepared/FS_INIT or wrong cycle count."""
    expected_cycles = SMOKE_CYCLES if smoke else MEASURED
    cmd = _main_bench_compile_command()
    mismatches: list[str] = []
    if not cmd:
        mismatches.append("compile_commands missing thermometer2 bench entry")
    forbidden = (
        "AETHER_DIAG_AETHER_HOT",
        "AE_EXP_PREPARED",
        "AETHER_POWER_BENCH_HOT",
        "AETHER_DIAG_AETHER_HOT_CACHED",
        "FS_INIT=",
    )
    for token in forbidden:
        if token in cmd:
            mismatches.append(f"forbidden macro in bench build: {token}")
    if "AE_EXP_THERMOMETER2_FULL_VANILLA=1" not in cmd and (
        "-DAE_EXP_THERMOMETER2_FULL_VANILLA=1" not in cmd
    ):
        mismatches.append("AE_EXP_THERMOMETER2_FULL_VANILLA not enabled")
    if f"AE_THERM_FULL_MEASURE_CYCLES={expected_cycles}" not in cmd:
        mismatches.append(
            f"AE_THERM_FULL_MEASURE_CYCLES != {expected_cycles} in compile line"
        )
    if "AE_THERM_FULL_VANILLA_SLEEP_S=20" not in cmd:
        mismatches.append("sleep_s not 20 in compile definitions")
    if f"SERVICE_UID={service_uid}" not in cmd and f"SERVICE_UID={service_uid.upper()}" not in cmd:
        if service_uid.lower() not in cmd.lower():
            mismatches.append(f"SERVICE_UID mismatch expected={service_uid}")
    patch_sha = file_sha256(AETHER_PATCH)
    if patch_sha and patch_sha != PATCH_SHA256_EXPECTED:
        mismatches.append(f"aether patch sha256 mismatch got={patch_sha}")
    if str(BUILD).replace("\\", "/").find("aether-hot") >= 0:
        mismatches.append(f"build dir looks like HOT profile: {BUILD}")
    sdk = BUILD / "sdkconfig"
    sdk_text = sdk.read_text(encoding="utf-8", errors="replace") if sdk.is_file() else ""
    audit = {
        "expected_cycles": expected_cycles,
        "expected_sleep_s": SLEEP_S,
        "service_uid": service_uid,
        "firmware_sha256": firmware_sha256(),
        "elf_path": str(BUILD / "temperature_sensor.elf"),
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
        ).strip(),
        "aether_patch_sha256": patch_sha,
        "mismatches": mismatches,
        "compile_audit": audit_aether_compile_macros(),
    }
    audit_path = RESULTS / (
        "build_profile_smoke.json" if smoke else "build_profile_measure.json"
    )
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    if mismatches:
        update_run_state(phase="CONFIG_MISMATCH", status="ABORTED", audit=audit)
        raise RuntimeError("CONFIG_MISMATCH: " + "; ".join(mismatches))
    return audit


def parse_bench_profile_banner(text: str) -> dict:
    out: dict[str, str | int] = {}
    if RE_BENCH_MODE.search(text):
        out["bench_mode"] = "FULL_VANILLA"
    m = RE_BENCH_CYCLES.search(text)
    if m:
        out["cycles"] = int(m.group(1))
    m = RE_BENCH_SLEEP.search(text)
    if m:
        out["sleep_s"] = int(m.group(1))
    m = RE_BENCH_SVC.search(text)
    if m:
        out["service_uid"] = m.group(1)
    m = RE_BENCH_HOT.search(text)
    if m:
        out["hot_prepared"] = int(m.group(1))
    return out


def verify_serial_bench_banner(text: str, *, service_uid: str, smoke: bool) -> None:
    banner = parse_bench_profile_banner(text)
    expected_cycles = SMOKE_CYCLES if smoke else MEASURED
    mismatches: list[str] = []
    if banner.get("bench_mode") != "FULL_VANILLA":
        mismatches.append("missing BENCH_MODE=FULL_VANILLA in serial banner")
    if banner.get("cycles") != expected_cycles:
        mismatches.append(
            f"banner cycles={banner.get('cycles')} expected={expected_cycles}"
        )
    if banner.get("sleep_s") != SLEEP_S:
        mismatches.append(f"banner sleep_s={banner.get('sleep_s')} expected={SLEEP_S}")
    if banner.get("hot_prepared") == 1:
        mismatches.append("hot_prepared=1 in banner")
    bs = banner.get("service_uid")
    if bs and str(bs).lower() != service_uid.lower():
        mismatches.append(f"banner service_uid={bs} expected={service_uid}")
    if mismatches:
        raise RuntimeError("CONFIG_MISMATCH serial: " + "; ".join(mismatches))


def preserve_confirmed_smoke_pass() -> None:
    src = CONFIRMED_SMOKE_PASS_RUN / "smoke_result.json"
    dst = RESULTS / "smoke_pass_run_20261017T160227Z.json"
    if src.is_file() and not dst.is_file():
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def append_usb_log(lines: list[str]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with USB_RECOVERY_LOG.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{ts} {line}\n")


def snapshot_usb_devices() -> list[str]:
    lines: list[str] = []
    sb.ensure_ppk_path()
    from serial.tools import list_ports

    for p in list_ports.comports():
        vid = f"{p.vid:04X}" if p.vid is not None else "----"
        pid = f"{p.pid:04X}" if p.pid is not None else "----"
        tag = "ESP" if p.vid == ESPRESSIF_VID else "other"
        lines.append(f"pyserial {tag} {p.device} VID={vid} PID={pid} {p.description}")
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_SerialPort | ForEach-Object { "
            "$_.DeviceID + ' PNP=' + $_.PNPDeviceID }",
        ],
        capture_output=True,
        text=True,
    )
    for line in (r.stdout or "").splitlines():
        if line.strip():
            lines.append(f"win32_serial {line.strip()}")
    r2 = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_303A|VID_1915' } | "
            "ForEach-Object { $_.Class + ' ' + $_.Status + ' ' + $_.InstanceId }",
        ],
        capture_output=True,
        text=True,
    )
    for line in (r2.stdout or "").splitlines():
        if line.strip():
            lines.append(f"pnp {line.strip()}")
    append_usb_log(lines)
    for line in lines:
        print(line, flush=True)
    return lines


def find_espressif_port_optional(*, verbose: bool = False) -> str | None:
    sb.ensure_ppk_path()
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == ESPRESSIF_VID:
            if verbose:
                print(
                    f"found ESPRESSIF {p.device} VID={p.vid:04X} PID={p.pid:04X} {p.description}",
                    flush=True,
                )
            return p.device
    return None


def verify_esptool_chip(port: str) -> dict:
    cmd = [
        str(sb.IDF_PYTHON),
        "-m",
        "esptool",
        "--chip",
        "esp32c6",
        "-p",
        port,
        "chip_id",
    ]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    append_usb_log([f"esptool chip_id port={port} rc={r.returncode}", out.strip()])
    ok = r.returncode == 0 and "ESP32-C6" in out
    return {"ok": ok, "rc": r.returncode, "output": out}


def stop_stage1_serial_holders() -> None:
    """Release PPK2 and ESP serial only for this stage-1 experiment."""
    sb.stop_external_ppk_holders()
    if sys.platform != "win32":
        return
    ps = r"""
Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -and (
      $_.CommandLine -match 'ppk2_hold_power|ppk2_log_power|ppk2_integrate_run|ppk2_power\.py' -or
      ($_.CommandLine -match 'idf_monitor|esp_idf_monitor|esptool' -and
       $_.CommandLine -match 'temperature-sensor-working-hot')
    )
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        check=False,
    )
    time.sleep(1.0)


def open_ppk_session() -> sb.PpkSession:
    last_err = "no attempt"
    for i in range(5):
        stop_stage1_serial_holders()
        time.sleep(1.5)
        try:
            ppk = sb.PpkSession(VOLTAGE_MV)
            ppk.open()
            return ppk
        except Exception as e:
            last_err = str(e)
            append_usb_log([f"ppk_open_fail attempt={i + 1} err={e}"])
            time.sleep(2.0)
    raise RuntimeError(f"PPK2 open failed: {last_err}")


def cold_boot_dut(ppk: sb.PpkSession, *, off_s: float = 4.0, wait_com_s: float = 60.0) -> str | None:
    """PPK power cycle for ESP_RST_POWERON so firmware keeps the 60s USB awake window."""
    append_usb_log(["cold_boot_dut begin"])
    ppk.set_off()
    time.sleep(off_s)
    append_usb_log([f"cold_boot_dut OFF held {off_s}s"])
    ppk.set_on()
    append_usb_log([f"cold_boot_dut ON mv={VOLTAGE_MV}"])
    snapshot_usb_devices()
    stop_stage1_serial_holders()
    time.sleep(0.5)
    port = sb.wait_espressif_com(timeout_s=wait_com_s)
    if port:
        append_usb_log([f"cold_boot_dut COM={port}"])
    else:
        append_usb_log(["cold_boot_dut NO_COM"])
    return port


def recover_espressif_usb(ppk: sb.PpkSession, *, attempts: int = 3, wait_s: float = 60.0) -> str | None:
    snapshot_usb_devices()
    for i in range(attempts):
        append_usb_log([f"=== power_cycle attempt {i + 1}/{attempts} ==="])
        ppk.set_off()
        time.sleep(4.0)
        append_usb_log(["DUT_POWER=OFF held 4s"])
        ppk.set_on()
        append_usb_log([f"DUT_POWER=ON mv={VOLTAGE_MV}"])
        snapshot_usb_devices()
        port = sb.wait_espressif_com(timeout_s=wait_s)
        if port:
            stop_stage1_serial_holders()
            time.sleep(0.8)
            chip = verify_esptool_chip(port)
            if chip["ok"]:
                append_usb_log([f"ESPRESSIF_COM_CONFIRMED={port}"])
                return port
            append_usb_log([f"esptool_verify_fail port={port}"])
        else:
            append_usb_log(["NO_ESPRESSIF_COM within timeout"])
    return None


def flash_firmware(port: str) -> None:
    sb.flash_port(port, BUILD)
    sb.hard_reset(port)


def save_build_provenance(
    *, service_uid: str, smoke: bool, aether_meta: dict | None = None
) -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    diff = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    ae_diff = ""
    if AETHER_LOCAL.is_dir():
        r = subprocess.run(["git", "diff"], cwd=str(AETHER_LOCAL), capture_output=True, text=True)
        ae_diff = r.stdout or ""
        if not ae_diff.strip():
            r2 = subprocess.run(["git", "status", "--short"], cwd=str(AETHER_LOCAL), capture_output=True, text=True)
            ae_diff = r2.stdout or ""
    bin_path = BUILD / "temperature_sensor.bin"
    elf_path = BUILD / "temperature_sensor.elf"
    aether_meta = aether_meta or {}
    meta = {
        "git_head": head,
        "git_diff_len": len(diff.stdout or ""),
        "aether_pin": AETHER_PIN,
        "aether_patch_sha256": aether_meta.get("aether_patch_sha256")
        or file_sha256(AETHER_PATCH),
        "aether_patch_path": str(AETHER_PATCH),
        "aether_head_at_build": aether_meta.get("aether_head"),
        "aether_local_diff_len": len(ae_diff),
        "receiver_source_root": str(RX_SRC),
        "receiver_build_stamp": str(RX_STAMP),
        "receiver_exe_sha256": file_sha256(RX_BUILD / "aether_hot_tcp_receiver.exe"),
        "firmware_sha256": firmware_sha256(),
        "elf_path": str(elf_path),
        "service_uid": service_uid,
        "smoke_build": smoke,
        "cycles": SMOKE_CYCLES if smoke else MEASURED,
        "user_config": str(USER_CONFIG_VANILLA),
        "build_dir": str(BUILD),
        "aether_compile_audit": audit_aether_compile_macros(),
    }
    (RESULTS / "build_provenance.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def capture_serial_resilient(
    seconds: int,
    out: Path,
    *,
    stop_when: Callable[[str], bool] | None = None,
    tick: Callable[[], None] | None = None,
) -> str:
    """Capture ESP USB-serial; reopen pyserial when COM reappears after deep sleep."""
    import serial  # type: ignore

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")
    buf = ""
    end = time.time() + seconds
    last_port = ""
    ser: serial.Serial | None = None

    def close_ser() -> None:
        nonlocal ser
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            ser = None

    try:
        while time.time() < end:
            if tick is not None:
                tick()
            port = find_espressif_port_optional()
            if port and port != last_port:
                close_ser()
                last_port = port
                append_usb_log([f"serial_capture_start port={port}"])
                try:
                    ser = serial.Serial(port, 115200, timeout=0.25)
                except Exception as exc:
                    append_usb_log([f"serial_open_fail port={port} err={exc!r}"])
                    ser = None
                    last_port = ""
                    time.sleep(0.3)
                    continue
            if ser is not None:
                try:
                    data = ser.read(8192)
                except Exception as exc:
                    append_usb_log([f"serial_read_fail port={last_port} err={exc!r}"])
                    close_ser()
                    last_port = ""
                    time.sleep(0.2)
                    continue
                if data:
                    text = data.decode("utf-8", errors="replace")
                    buf += text
                    with out.open("a", encoding="utf-8", errors="replace") as f:
                        f.write(text)
            if stop_when and stop_when(buf):
                break
            if not port:
                last_port = ""
                close_ser()
                time.sleep(0.2)
            else:
                time.sleep(0.05)
    finally:
        close_ser()
    return buf


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    return float(np.percentile(np.asarray(vals, dtype=float), p))


def agg_stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "p95": None, "min": None, "max": None}
    return {
        "n": len(vals),
        "mean": float(statistics.mean(vals)),
        "median": float(statistics.median(vals)),
        "p95": pct(vals, 95),
        "min": float(min(vals)),
        "max": float(max(vals)),
    }


def _journal_row(m: re.Match[str]) -> dict:
    return {
        "run_id": int(m.group(2)),
        "got_ip": int(m.group(3)),
        "select_ok": int(m.group(4)),
        "write_called": int(m.group(5)),
        "write_status": int(m.group(6)),
        "fail_stage": int(m.group(7)),
        "active_ms": int(m.group(8)),
    }


def parse_journal(text: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for m in RE_JOURNAL.finditer(text):
        out[int(m.group(1))] = _journal_row(m)
    return out


def parse_journal_merged(text: str) -> dict[int, dict]:
    out = parse_journal(text)
    for m in RE_JOURNAL_DUMP.finditer(text):
        out[int(m.group(1))] = _journal_row(m)
    return out


def journal_field_status(entry: dict | None) -> str:
    return "UNKNOWN" if not entry else "known"


def load_raw_slice(raw_bin: Path, start: int, end: int) -> tuple[np.ndarray, np.ndarray]:
    count = max(0, end - start)
    if count <= 0:
        return np.array([]), np.array([])
    with raw_bin.open("rb") as f:
        f.seek(start * 4)
        u = np.fromfile(f, dtype=np.float32, count=count)
    t = (np.arange(start, start + len(u), dtype=np.float64)) / FS_HZ
    return t, u.astype(np.float64)


def integrate_window(raw_bin: Path, w: CycleWindow) -> dict:
    t, u = load_raw_slice(raw_bin, w.wake_start_sample, w.wake_end_sample)
    invalid_reason = ""
    valid = True
    if w.max_gap_samples >= 500:
        valid = False
        invalid_reason = "INVALID_ACQUISITION_GAP"
    if w.close_reason == CloseReason.FORCE_LIMIT:
        valid = False
        invalid_reason = invalid_reason or "force_limit_clip"
    elif w.close_reason == CloseReason.FINALIZE:
        valid = False
        invalid_reason = invalid_reason or "finalize_no_sleep_edge"
    duration_s = w.wake_end_s - w.wake_start_s
    charge_mC = 0.0
    peak_mA = 0.0
    avg_mA = 0.0
    if len(t) >= 2:
        try:
            integ = float(np.trapezoid(u, t))
        except AttributeError:
            integ = float(np.trapz(u, t))
        charge_mC = abs(integ) / 1000.0
        peak_mA = float(np.max(np.abs(u))) / 1000.0
        avg_mA = (charge_mC / duration_s) if duration_s > 0 else 0.0
    return {
        "ppk_sequence": w.sequence,
        "duration_ms": duration_s * 1000.0,
        "charge_mC": charge_mC,
        "energy_mJ": charge_mC * VOLTAGE_V,
        "mean_current_mA": avg_mA,
        "peak_current_mA": peak_mA,
        "valid": valid,
        "invalid_reason": invalid_reason,
        "close_reason": str(w.close_reason.value),
    }


def measured_ppk_windows(windows: list[CycleWindow], n: int) -> list[CycleWindow]:
    ordered = sorted(windows, key=lambda w: w.wake_start_s)
    if len(ordered) >= n:
        return ordered[:n]
    return ordered


def build_measure_rows(
    windows: list[CycleWindow],
    raw_bin: Path,
    journal: dict[int, dict],
    rx_run_ids: set[int],
    *,
    ordered_rx: list[int] | None = None,
) -> list[dict]:
    ppk_w = measured_ppk_windows(windows, MEASURED)
    rows: list[dict] = []
    prev_end: float | None = None
    for i, w in enumerate(ppk_w, start=1):
        base = integrate_window(raw_bin, w)
        j = journal.get(i)
        run_id: int | None = None
        if j and j.get("run_id") is not None:
            run_id = int(j["run_id"])
        elif ordered_rx and i <= len(ordered_rx):
            run_id = ordered_rx[i - 1]
        sleep_d = w.wake_start_s - prev_end if prev_end is not None else None
        sleep_charge = None
        if prev_end is not None and sleep_d and sleep_d > 0:
            t0 = int(prev_end * FS_HZ)
            t1 = int(w.wake_start_s * FS_HZ)
            ts, us = load_raw_slice(raw_bin, t0, t1)
            if len(ts) >= 2:
                try:
                    sleep_charge = abs(float(np.trapezoid(us, ts))) / 1000.0
                except AttributeError:
                    sleep_charge = abs(float(np.trapz(us, ts))) / 1000.0
        if j is None:
            write_ok: bool | None = None
        else:
            write_ok = int(j.get("write_status", 255)) == 0
        rx = run_id in rx_run_ids if run_id is not None else False
        rows.append(
            {
                "cycle": i,
                "run_id": run_id if run_id is not None else "",
                **base,
                "sleep_duration_s": sleep_d,
                "sleep_charge_mC": sleep_charge,
                "journal_status": journal_field_status(j),
                "got_ip": j.get("got_ip") if j else None,
                "select_ok": j.get("select_ok") if j else None,
                "write_called": j.get("write_called") if j else None,
                "write_terminal_ok": write_ok,
                "fail_stage": j.get("fail_stage") if j else None,
                "receiver_received": rx,
                "active_ms_esp": j.get("active_ms") if j else None,
            }
        )
        prev_end = w.wake_end_s
    return rows


class ContinuousPpkCapture:
    def __init__(self, ppk: sb.PpkSession, raw_bin: Path, meta_path: Path) -> None:
        self.ppk = ppk
        self.raw_bin = raw_bin
        self.meta_path = meta_path
        self.bin_f = raw_bin.open("wb")
        self.cfg = thermometer2_full_vanilla_segmenter_config(cycles=MEASURED)
        self.segmenter = StreamingCycleSegmenter(self.cfg)
        self.sample_count = 0
        self.wall_start = 0.0

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
            "voltage_mV": VOLTAGE_MV,
            "sample_count": self.sample_count,
            "max_wake_s": self.cfg.max_wake_s,
            "sleep_s": SLEEP_S,
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


def bootstrap_write_run_id(esp_text: str) -> int | None:
    if "BOOTSTRAP_FINISH_BEGIN" not in esp_text:
        return None
    head = esp_text.split("BOOTSTRAP_FINISH_BEGIN", 1)[0]
    terminals = RE_WRITE_TERMINAL.findall(head)
    if not terminals:
        return None
    return int(terminals[-1][1])


def measured_run_ids(esp_text: str, *, expected_cycles: int) -> list[int]:
    journal = parse_journal_merged(esp_text)
    from_journal = [
        int(journal[c]["run_id"])
        for c in range(1, expected_cycles + 1)
        if c in journal and journal[c].get("run_id")
    ]
    if len(from_journal) >= expected_cycles:
        return from_journal[:expected_cycles]
    boot_ids = [int(br) for bc, br in RE_BOOT_RUN.findall(esp_text)]
    if len(boot_ids) >= expected_cycles:
        return boot_ids[:expected_cycles]
    bootstrap_rid = bootstrap_write_run_id(esp_text)
    seen: list[int] = []
    for _, rid in RE_WRITE_TERMINAL.findall(esp_text):
        run_id = int(rid)
        if bootstrap_rid is not None and run_id == bootstrap_rid:
            continue
        if run_id not in seen:
            seen.append(run_id)
    if len(seen) >= expected_cycles:
        return seen[-expected_cycles:]
    return from_journal


def parse_smoke_detailed(
    esp_text: str, rx_text: str, *, expected_cycles: int = SMOKE_CYCLES
) -> dict:
    journal = parse_journal_merged(esp_text)
    expected_run_ids = measured_run_ids(esp_text, expected_cycles=expected_cycles)
    write_terminal_ids = [int(rid) for _, rid in RE_WRITE_TERMINAL.findall(esp_text)]
    bootstrap_rid = bootstrap_write_run_id(esp_text)
    rx_hits: list[int] = []
    dup = 0
    seen_rx: set[int] = set()
    for m in RE_FULL_RX.finditer(rx_text):
        rid = int(m.group(1))
        if rid not in expected_run_ids:
            continue
        rx_hits.append(rid)
        if rid in seen_rx:
            dup += 1
        seen_rx.add(rid)
    matched = sorted(seen_rx)
    panic = "Guru Meditation" in esp_text or "assert failed" in esp_text
    cycles_logged = sum(1 for c in range(1, expected_cycles + 1) if c in journal)
    bootstrap_ok = (
        "BOOTSTRAP_DONE" in esp_text
        or "SENDER_UID_RTC stored=" in esp_text
        or ("BOOTSTRAP_SAVE" in esp_text and "DEEP_SLEEP_ENTER" in esp_text)
    )
    select_ok_count = len(RE_SELECT_OK.findall(esp_text))
    sender_uids = RE_SENDER_UID_RTC.findall(esp_text)
    sender_uid_after_sleep = sender_uids[-1] if sender_uids else None
    if cycles_logged >= expected_cycles:
        select_ok_per_cycle = all(
            journal.get(c, {}).get("select_ok") == 1
            for c in range(1, expected_cycles + 1)
            if c in journal
        )
    elif journal:
        select_ok_per_cycle = all(
            journal.get(c, {}).get("select_ok") == 1
            for c in range(1, expected_cycles + 1)
            if c in journal
        ) and len(journal) >= expected_cycles
    else:
        select_ok_per_cycle = select_ok_count >= expected_cycles
    journal_complete = all(c in journal for c in range(1, expected_cycles + 1))
    return {
        "expected_run_ids": expected_run_ids,
        "matched_rx_run_ids": matched,
        "full_rx_count": len(matched),
        "duplicate_rx": dup,
        "journal_cycles": cycles_logged,
        "bootstrap_ok": bootstrap_ok,
        "select_ok_events": select_ok_count,
        "sender_uid_stored": sender_uid_after_sleep,
        "panic": panic,
        "bootstrap_write_run_id": bootstrap_rid,
        "journal_complete": journal_complete,
        "pass": (
            bootstrap_ok
            and journal_complete
            and len(expected_run_ids) >= expected_cycles
            and len(matched) >= expected_cycles
            and dup == 0
            and not panic
            and select_ok_per_cycle
            and all(r in matched for r in expected_run_ids[:expected_cycles])
            and (
                bootstrap_rid is None
                or bootstrap_rid not in expected_run_ids[:expected_cycles]
            )
        ),
    }


def parse_smoke(esp_log: Path, rx_log: Path) -> dict:
    esp = esp_log.read_text(encoding="utf-8", errors="replace") if esp_log.is_file() else ""
    rx_parts: list[str] = []
    prev = rx_log.with_suffix(".log.prev")
    if prev.is_file():
        rx_parts.append(prev.read_text(encoding="utf-8", errors="replace"))
    if rx_log.is_file():
        rx_parts.append(rx_log.read_text(encoding="utf-8", errors="replace"))
    rx = "\n".join(rx_parts)
    return parse_smoke_detailed(esp, rx, expected_cycles=SMOKE_CYCLES)


def firmware_sha256() -> str:
    bin_path = BUILD / "temperature_sensor.bin"
    if not bin_path.is_file():
        return ""
    return hashlib.sha256(bin_path.read_bytes()).hexdigest()


def write_measure_artifacts(
    rows: list[dict],
    meta: dict,
    *,
    csv_out: Path = CSV_OUT,
    json_out: Path = JSON_OUT,
) -> None:
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "cycle",
        "run_id",
        "duration_ms",
        "charge_mC",
        "energy_mJ",
        "mean_current_mA",
        "peak_current_mA",
        "sleep_duration_s",
        "sleep_charge_mC",
        "got_ip",
        "select_ok",
        "write_called",
        "write_terminal_ok",
        "fail_stage",
        "receiver_received",
        "valid",
        "invalid_reason",
        "close_reason",
    ]
    with csv_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    json_out.write_text(json.dumps({"meta": meta, "cycles": rows}, indent=2), encoding="utf-8")
    rx_n = sum(1 for r in rows if r.get("receiver_received"))
    valid_ppk = sum(1 for r in rows if r.get("valid"))
    MD_OUT.write_text(
        "\n".join(
            [
                "# FULL vanilla Thermometer2 (stage 1)",
                "",
                f"- RX unique / {MEASURED}: {rx_n}/{MEASURED}",
                f"- PPK complete windows: {valid_ppk}/{MEASURED}",
                f"- Git: {meta.get('git_head')}",
                f"- Firmware SHA256: {meta.get('firmware_sha256')}",
                "",
                json.dumps(meta.get("wifi_config", {}), indent=2),
            ]
        ),
        encoding="utf-8",
    )


def count_valid_ppk_windows(windows: list[CycleWindow]) -> int:
    ordered = measured_ppk_windows(windows, MEASURED)
    n = 0
    for w in ordered:
        if w.close_reason == CloseReason.SLEEP_EDGE:
            n += 1
    return n


def smoke_service_uid() -> str:
    path = RESULTS / "smoke_result.json"
    if not path.is_file():
        raise RuntimeError("smoke_result.json missing; run --smoke first")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("pass"):
        raise RuntimeError("smoke did not pass; refusing --measure")
    uid = data.get("receiver_uid")
    if not uid:
        raise RuntimeError("smoke_result.json missing receiver_uid")
    return str(uid)


def run_measure(
    uid: str,
    ssid: str,
    pwd: str,
    rx_log: Path,
    rx_proc: subprocess.Popen,
    *,
    service_uid_locked: str | None = None,
    ppk: sb.PpkSession | None = None,
    campaign_dir: Path | None = None,
) -> int:
    service_uid = service_uid_locked or smoke_service_uid()
    if uid.lower() != service_uid.lower():
        print(
            f"WARN receiver_uid={uid} != smoke SERVICE_UID={service_uid}; "
            "flashing firmware with smoke SERVICE_UID (required for ESP client identity).",
            flush=True,
        )
    out_base = campaign_dir if campaign_dir else RESULTS
    raw_dir = out_base / "ppk_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_bin = raw_dir / "full_vanilla_100.bin"
    meta_bin = raw_bin.with_suffix(".json")
    build_esp(service_uid=service_uid, ssid=ssid, password=pwd, smoke=False)
    verify_build_profile(service_uid=service_uid, smoke=False)
    prov = save_build_provenance(
        service_uid=service_uid, smoke=False, aether_meta=ensure_aether_patched()
    )
    update_run_state(
        phase="measure",
        status="RUNNING",
        service_uid=service_uid,
        measure_firmware_sha256=prov.get("firmware_sha256"),
    )
    if campaign_dir:
        (campaign_dir / "measure_build_manifest.json").write_text(
            json.dumps(prov, indent=2), encoding="utf-8"
        )
    ensure_receiver_alive(rx_proc, uid, rx_log)
    own_ppk = ppk is None
    if own_ppk:
        ppk = open_ppk_session()
    assert ppk is not None
    try:
        port = recover_espressif_usb(ppk, attempts=3, wait_s=60.0)
        if not port:
            _write_blocked_handoff("USB_ACCESS=BLOCKED", uid, prov)
            return 2
        flash_firmware(port)
        cap = ContinuousPpkCapture(ppk, raw_bin, meta_bin)
        cap.start()
        sb.hard_reset(port)
        # Quiet measure firmware may omit USB journal; PPK sleep edges are authoritative.
        deadline = time.time() + COLD_BOOT_S + MEASURED * (120.0 + SLEEP_S) + 1200.0
        rx_mark = rx_log.stat().st_size if rx_log.is_file() else 0
        esp_log = out_base / "measure_esp.log"
        esp_log.write_text("", encoding="utf-8")
        journal_done = False
        while time.time() < deadline:
            cap.drain()
            ppk_n = count_valid_ppk_windows(cap.segmenter.windows_closed)
            if not journal_done and ppk_n >= min(MEASURED, 10):
                capture_serial_resilient(
                    180,
                    esp_log,
                    stop_when=lambda b: "CAMPAIGN_DONE" in b
                    or b.count("JOURNAL_DUMP cycle=") >= MEASURED,
                    tick=lambda: cap.drain(),
                )
                journal_done = True
            if ppk_n >= MEASURED:
                post_collect_receiver(
                    rx_proc,
                    uid,
                    rx_log,
                    SMOKE_RX_POST_COLLECT_S,
                    tick=lambda: cap.drain(),
                )
                break
            time.sleep(0.002)
        cap.stop()
        esp_text = esp_log.read_text(encoding="utf-8", errors="replace") if esp_log.is_file() else ""
        journal = parse_journal_merged(esp_text)
        rx_text = ""
        if rx_log.is_file():
            with rx_log.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(rx_mark)
                rx_text = f.read()
        ordered_rx = [int(x) for x in RE_FULL_RX.findall(rx_text)]
        rx_ids = set(ordered_rx)
        measure_full_rx = len(rx_ids)
        rows = build_measure_rows(
            cap.segmenter.windows_closed,
            raw_bin,
            journal,
            rx_ids,
            ordered_rx=ordered_rx,
        )
        valid_ppk = count_valid_ppk_windows(cap.segmenter.windows_closed)
        git_head = prov["git_head"]
        meta = {
            "git_head": git_head,
            "firmware_sha256": prov["firmware_sha256"],
            "aether_pin": AETHER_PIN,
            "receiver_uid": uid,
            "ppk_windows_total": len(cap.segmenter.windows_closed),
            "ppk_sleep_edge_windows": valid_ppk,
            "journal_cycles": len(journal),
            "wifi_config": {
                "AE_EXP_WIFI_CANONICAL": 1,
                "AE_EXP_WIFI_FEAT_*": "all unset/0",
                "WIFI_PS": "WIFI_PS_NONE",
                "DHCP": True,
                "sleep_s": SLEEP_S,
            },
            "PREPARED_USED": "no",
            "HOT_USED": "no",
            "WIFI_CACHE_USED": "no",
        }
        csv_out = out_base / "thermometer2_full_vanilla_100.csv"
        json_out = out_base / "thermometer2_full_vanilla_100.json"
        write_measure_artifacts(rows, meta, csv_out=csv_out, json_out=json_out)
        rx_n = sum(1 for r in rows if r.get("receiver_received"))
        active_charge = [r["charge_mC"] for r in rows if r.get("valid")]
        sleep_charge = [r["sleep_charge_mC"] for r in rows if r.get("sleep_charge_mC") is not None]
        summary = {
            "rx": rx_n,
            "full_rx_unique_in_measure_log": measure_full_rx,
            "ppk_sleep_edge": valid_ppk,
            "charge_stats": agg_stats(active_charge),
            "energy_stats": agg_stats([r["energy_mJ"] for r in rows if r.get("valid")]),
            "sleep_charge_stats": agg_stats(sleep_charge),
        }
        summary_path = out_base / "measure_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (RESULTS / "measure_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2), flush=True)
        ok = valid_ppk >= MEASURED and measure_full_rx >= MEASURED
        return 0 if ok else 1
    finally:
        if own_ppk:
            ppk.set_off()


def _write_blocked_handoff(usb_status: str, uid: str, prov: dict) -> None:
    rep = {
        "USB_ACCESS": usb_status,
        "SMOKE": "NOT_RUN",
        "MEASURE": "NOT_RUN",
        "receiver_uid": uid,
        "git_head": prov.get("git_head"),
        "firmware_sha256": prov.get("firmware_sha256"),
        "manual_action": "Connect Thermometer 2 USB to host (or fix DUT wiring); ensure PPK2 sources 3V and DUT VDD/USB share ground.",
    }
    (RESULTS / "STAGE1_HANDOFF.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(json.dumps(rep, indent=2), flush=True)


def smoke_serial_complete(buf: str) -> bool:
    j = parse_journal_merged(buf)
    have_j = sum(1 for c in (1, 2, 3) if c in j) >= 3
    have_recap = "JOURNAL_RECAP count=3" in buf and buf.count("JOURNAL_DUMP cycle=") >= 3
    if not (have_j or have_recap):
        return False
    tail = buf.rfind("JOURNAL cycle=3 ")
    if tail < 0:
        tail = buf.rfind("JOURNAL_DUMP cycle=3 ")
    after = buf[tail:] if tail >= 0 else buf
    return "DEEP_SLEEP_ENTER" in after or "NETWORK_STOPPED" in after


def post_collect_receiver(
    rx_proc: subprocess.Popen,
    uid: str,
    rx_log: Path,
    seconds: float,
    *,
    tick: Callable[[], None] | None = None,
) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if tick is not None:
            tick()
        ensure_receiver_alive(rx_proc, uid, rx_log)
        time.sleep(0.5)


def run_smoke(
    uid: str,
    ssid: str,
    pwd: str,
    rx_log: Path,
    rx_proc: subprocess.Popen,
    *,
    ppk: sb.PpkSession | None = None,
    run_dir: Path | None = None,
) -> int:
    run_dir = new_smoke_run_dir(run_dir)
    esp_log = run_dir / "smoke_esp.log"
    rx_log_run = run_dir / "receiver.log"
    aether_meta = ensure_aether_patched()
    build_esp(service_uid=uid, ssid=ssid, password=pwd, smoke=True)
    verify_build_profile(service_uid=uid, smoke=True)
    prov = save_build_provenance(service_uid=uid, smoke=True, aether_meta=aether_meta)
    update_run_state(
        phase="smoke",
        status="RUNNING",
        service_uid=uid,
        firmware_sha256=prov.get("firmware_sha256"),
    )
    ensure_receiver_alive(rx_proc, uid, rx_log)
    own_ppk = ppk is None
    if own_ppk:
        ppk = open_ppk_session()
    assert ppk is not None
    try:
        try:
            port = recover_espressif_usb(ppk, attempts=3, wait_s=60.0)
            if not port:
                _write_blocked_handoff("USB_ACCESS=BLOCKED", uid, prov)
                return 2
            flash_firmware(port)
            ensure_receiver_alive(rx_proc, uid, rx_log)
            port = cold_boot_dut(ppk) or port
            if not port:
                _write_blocked_handoff("USB_ACCESS=BLOCKED after flash cold boot", uid, prov)
                return 2
            banner_log = run_dir / "bench_banner.log"
            capture_serial_resilient(
                90,
                banner_log,
                stop_when=lambda b: "BENCH_MODE=FULL_VANILLA" in b,
            )
            banner_text = (
                banner_log.read_text(encoding="utf-8", errors="replace")
                if banner_log.is_file()
                else ""
            )
            verify_serial_bench_banner(banner_text, service_uid=uid, smoke=True)
            esp_log.write_text(banner_text, encoding="utf-8")

            def smoke_done(buf: str) -> bool:
                return smoke_serial_complete(buf)

            capture_serial_resilient(
                int(SMOKE_CAPTURE_MAX_S),
                esp_log,
                stop_when=smoke_done,
            )
            post_collect_receiver(rx_proc, uid, rx_log, SMOKE_RX_POST_COLLECT_S)
            if rx_log.is_file():
                rx_log_run.write_text(
                    rx_log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
                )
            rx_text = (
                rx_log_run.read_text(encoding="utf-8", errors="replace")
                if rx_log_run.is_file()
                else ""
            )
            esp_text = (
                esp_log.read_text(encoding="utf-8", errors="replace") if esp_log.is_file() else ""
            )
            res = parse_smoke_detailed(esp_text, rx_text, expected_cycles=SMOKE_CYCLES)
            res["firmware_sha256"] = prov["firmware_sha256"]
            res["receiver_uid"] = uid
            res["receiver_pid"] = rx_proc.pid
            res["usb_port"] = port
            res["registration_enabled"] = True
            res["run_dir"] = str(run_dir)
            res["run_id"] = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))[
                "run_id"
            ]
            (run_dir / "smoke_build_manifest.json").write_text(
                json.dumps(prov, indent=2), encoding="utf-8"
            )
            (run_dir / "smoke_result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
            (RESULTS / "smoke_result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
            if res.get("pass"):
                (run_dir / "smoke_pass.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
                (RESULTS / "smoke_pass_run.json").write_text(
                    json.dumps(res, indent=2), encoding="utf-8"
                )
            handoff = {
                "git_head": prov.get("git_head"),
                "registration_enabled": True,
                "bootstrap_ok": res.get("bootstrap_ok"),
                "sender_uid_stored": res.get("sender_uid_stored"),
                "receiver_alive": rx_proc.poll() is None,
                "receiver_uid": uid,
                "smoke_rx": f"{res.get('full_rx_count', 0)}/3",
                "smoke_pass": res.get("pass"),
                "artifacts": str(RESULTS),
            }
            (RESULTS / "STAGE1_HANDOFF.json").write_text(
                json.dumps(handoff, indent=2), encoding="utf-8"
            )
            print(json.dumps(res, indent=2), flush=True)
            update_run_state(
                phase="smoke_done",
                status="PASS" if res.get("pass") else "FAIL",
                smoke_rx=f"{res.get('full_rx_count', 0)}/3",
            )
            return 0 if res.get("pass") else 1
        except Exception as exc:
            update_run_state(phase="smoke", status="ABORTED", error=str(exc))
            raise
    finally:
        if own_ppk:
            ppk.set_off()


def run_combined_campaign(
    uid: str, ssid: str, pwd: str, rx_log: Path, rx_proc: subprocess.Popen
) -> int:
    campaign_dir, campaign_id = new_campaign_run_dir()
    deadline = time.time() + BOOTSTRAP_MAX_S + SMOKE_CAPTURE_MAX_S + MEASURE_DEADLINE_S
    update_run_state(
        phase="campaign",
        status="RUNNING",
        campaign_id=campaign_id,
        deadline_utc=datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
        run_dir=str(campaign_dir),
    )
    ppk = open_ppk_session()
    try:
        smoke_dir = campaign_dir / "smoke"
        rc = run_smoke(
            uid, ssid, pwd, rx_log, rx_proc, ppk=ppk, run_dir=smoke_dir
        )
        if rc != 0:
            (campaign_dir / "campaign_result.json").write_text(
                json.dumps({"campaign_id": campaign_id, "smoke": "FAIL", "measure": "NOT_RUN"}, indent=2),
                encoding="utf-8",
            )
            update_run_state(phase="campaign", status="FAIL", measure="NOT_RUN")
            return rc
        if time.time() > deadline:
            update_run_state(phase="campaign", status="TIMEOUT", at="before_measure")
            return 4
        rc = run_measure(
            uid,
            ssid,
            pwd,
            rx_log,
            rx_proc,
            service_uid_locked=uid,
            ppk=ppk,
            campaign_dir=campaign_dir,
        )
        (campaign_dir / "campaign_result.json").write_text(
            json.dumps(
                {
                    "campaign_id": campaign_id,
                    "smoke": "PASS",
                    "measure": "PASS" if rc == 0 else "FAIL",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        update_run_state(
            phase="campaign_done",
            status="PASS" if rc == 0 else "FAIL",
            measure="PASS" if rc == 0 else "FAIL",
        )
        return rc
    finally:
        ppk.set_off()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--measure", action="store_true", help="100-cycle PPK measure (after smoke)")
    ap.add_argument("--flash", action="store_true", default=True)
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    preserve_confirmed_smoke_pass()
    archive_wrong_profile_hot_run()
    stop_stage1_serial_holders()
    aether_meta = ensure_aether_patched()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    update_run_state(phase="init", status="STARTING", git_head=head)
    RESULTS.mkdir(parents=True, exist_ok=True)
    audit_path = RESULTS / "client_config_audit.json"
    if not audit_path.is_file():
        audit_path.write_text(
            json.dumps(
                {
                    "before_change": {
                        "user_config": "main/user_config_full_quiet.h",
                        "AE_SUPPORT_REGISTRATION": 0,
                        "AE_SUPPORT_CLOUD_DNS": 0,
                        "AE_SUPPORT_TCP": 0,
                        "AE_SUPPORT_UDP": 1,
                        "FS_INIT": "experiments/preprovision/sender_fs_157aadbe.h",
                        "select_err_observed": 2,
                        "select_err_meaning": "no clients in map (registration disabled)",
                    },
                    "after_change": {
                        "user_config": str(USER_CONFIG_VANILLA.relative_to(ROOT)),
                        "FS_INIT": "not_used",
                        "build_dir": str(BUILD.relative_to(ROOT)),
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    (RESULTS / "branch_head.json").write_text(
        json.dumps(
            {
                "branch": "diag/thermometer2-aether-full-vanilla",
                "head": head,
                **aether_meta,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    ssid, pwd = load_wifi()
    if args.build_only:
        build_esp(
            service_uid="00000000-0000-0000-0000-000000000001",
            ssid=ssid,
            password=pwd,
            smoke=args.smoke or args.measure,
        )
        print(f"FIRMWARE_SHA256={firmware_sha256()}", flush=True)
        print("NOTE=build-only uses placeholder SERVICE_UID; do not flash this binary.", flush=True)
        return 0

    try:
        acquire_runner_lock()
    except RuntimeError as e:
        print(str(e), flush=True)
        return 3

    exe = build_receiver()
    rx_log = RESULTS / "receiver.log"
    session = HERE / "thermometer2_full_vanilla_rx_session"
    proc = start_receiver(exe, session)
    try:
        uid = wait_uid(rx_log)
        print(f"RECEIVER_UID={uid}", flush=True)
        if args.smoke and args.measure:
            return run_combined_campaign(uid, ssid, pwd, rx_log, proc)
        if args.smoke:
            return run_smoke(uid, ssid, pwd, rx_log, proc)
        if args.measure:
            smoke_json = RESULTS / "smoke_result.json"
            if smoke_json.is_file():
                prev = json.loads(smoke_json.read_text(encoding="utf-8"))
                if not prev.get("pass"):
                    print("SMOKE_NOT_PASS=1 refusing --measure", flush=True)
                    return 1
            else:
                print("SMOKE_MISSING=1 refusing --measure", flush=True)
                return 1
            ensure_receiver_alive(proc, uid, rx_log)
            return run_measure(uid, ssid, pwd, rx_log, proc)
        print("Specify --smoke and/or --measure", flush=True)
        return 2
    finally:
        release_runner_lock()


if __name__ == "__main__":
    raise SystemExit(main())
