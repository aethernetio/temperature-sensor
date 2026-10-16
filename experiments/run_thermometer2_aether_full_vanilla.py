#!/usr/bin/env python3
"""Thermometer 2: full Æther + vanilla Wi-Fi deep-sleep benchmark runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = ROOT / "build-thermometer2-aether-full-vanilla"
RESULTS = HERE / "thermometer2_full_vanilla_results"
PREPARED_ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
RX_SRC = PREPARED_ROOT / "temperature_receiver"
RX_BUILD = HERE / "thermometer2_full_vanilla_rx_build"
AETHER_LOCAL = ROOT / "deps" / "aether-client-cpp-157aadbe-diag"
FS_INIT = HERE / "preprovision" / "sender_fs_157aadbe.h"
AETHER_PIN = "157aadbec8e7b852d0f89274307ff7cb8103e5f7"
BENCH_CLIENT_ID = "reliability_full_v1"

RE_RX_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_FULL_RX = re.compile(r"AETHER_FULL_READY run_id=(\d+)")
RE_RUN_ID = re.compile(r"run_id=(\d+)")


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


def run_idf(extra: str) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(idf_ps(extra), cwd=str(ROOT), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"idf failed code={r.returncode}")


def build_esp(*, service_uid: str, ssid: str, password: str, smoke: bool, port: str, flash: bool) -> None:
    b = BUILD.as_posix()
    pwd = password.replace("'", "''")
    ssid_ps = ssid.replace("'", "''")
    fs = FS_INIT.as_posix() if FS_INIT.is_file() else ""
    aether = AETHER_LOCAL.as_posix() if AETHER_LOCAL.is_dir() else ""
    cycles = "3" if smoke else "100"
    stage = "1" if smoke else "0"
    sdk_defaults = (
        f"{ROOT.as_posix()}/sdkconfig.defaults;"
        f"{ROOT.as_posix()}/sdkconfig.defaults.thermometer2_aether_full_vanilla"
    )
    if smoke:
        sdk_defaults += f";{ROOT.as_posix()}/sdkconfig.defaults.bench"
    extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG_DEFAULTS={sdk_defaults}',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_THERMOMETER2_FULL_VANILLA=1',
  '-D','AE_EXP_WIFI_CANONICAL=1',
  '-D','AE_EXP_BENCH_STAGE={stage}',
  '-D','AE_THERM_FULL_MEASURE_CYCLES={cycles}',
  '-D','AE_THERM_FULL_VANILLA_SLEEP_S=20',
  '-D','AE_THERM_FULL_ACTIVE_TIMEOUT_MS=60000',
  '-D','CPM_aether-client-cpp_SOURCE={aether}',
  '-D','USER_CONFIG={ROOT.as_posix()}/main/user_config_full_quiet.h'
)
"""
    if fs:
        extra += f"$common += @('-D','FS_INIT={fs}')\n"
    extra += f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py @common reconfigure build
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    if flash:
        extra += f"""
idf.py @common -p {port} flash
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    run_idf(extra)


def build_receiver() -> Path:
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    ps = f"""
$ErrorActionPreference='Stop'
$vswhere = "${{env:ProgramFiles(x86)}}\\Microsoft Visual Studio\\Installer\\vswhere.exe"
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvars = Join-Path $vs 'VC\\Auxiliary\\Build\\vcvars64.bat'
$rxs = '{RX_SRC.as_posix()}'
$rxb = '{RX_BUILD.as_posix()}'
$ae = '{AETHER_LOCAL.as_posix()}'
cmd /c "`"$vcvars`" && cmake -S %rxs% -B %rxb% -G Ninja -DCMAKE_BUILD_TYPE=Release -DCPM_aether-client-cpp_SOURCE=%ae% && cmake --build %rxb% --target aether_hot_tcp_receiver --parallel"
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError("receiver build failed")
    exe = RX_BUILD / "aether_hot_tcp_receiver.exe"
    if not exe.is_file():
        raise RuntimeError(f"missing {exe}")
    return exe


def start_receiver(exe: Path, session: Path) -> subprocess.Popen:
    session.mkdir(parents=True, exist_ok=True)
    for f in session.glob("*"):
        if f.is_file():
            f.unlink()
    env = os.environ.copy()
    env["AE_RECEIVER_SESSION_DIR"] = str(session)
    log = RESULTS / "receiver.log"
    RESULTS.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [str(exe), "--require-tcp"],
        stdout=open(log, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        env=env,
    )


def wait_uid(log: Path, timeout: float = 300.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.is_file():
            m = RE_RX_UID.search(log.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
        time.sleep(1.0)
    raise RuntimeError("receiver UID timeout")


def find_com() -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_SerialPort | Where-Object { $_.PNPDeviceID -match 'VID_303A' } | Select-Object -ExpandProperty DeviceID -First 1"],
        capture_output=True,
        text=True,
    )
    port = (r.stdout or "").strip()
    if not port:
        raise RuntimeError("No Espressif COM port")
    return port


def capture(port: str, seconds: int, out: Path) -> None:
    elf = BUILD / "temperature_sensor.elf"
    mon = f"python $env:IDF_PATH/tools/idf_monitor.py -p {port} -b 115200 {elf.as_posix()}"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.Popen(
        idf_ps(mon),
        stdout=open(out, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        env=env,
    )
    try:
        end = time.time() + seconds
        while time.time() < end:
            if out.is_file() and "CAMPAIGN_DONE" in out.read_text(encoding="utf-8", errors="replace"):
                time.sleep(3)
                break
            time.sleep(2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def parse_smoke(esp_log: Path, rx_log: Path) -> dict:
    esp = esp_log.read_text(encoding="utf-8", errors="replace") if esp_log.is_file() else ""
    rx = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.is_file() else ""
    run_ids = set(RE_RUN_ID.findall(esp))
    full_rx = set(RE_FULL_RX.findall(rx))
    return {
        "esp_run_ids": sorted(run_ids, key=int),
        "full_rx_run_ids": sorted(full_rx, key=int),
        "full_rx_count": len(full_rx),
        "panic": "Guru Meditation" in esp or "assert failed" in esp,
    }


def firmware_sha256() -> str:
    bin_path = BUILD / "temperature_sensor.bin"
    if not bin_path.is_file():
        return ""
    return hashlib.sha256(bin_path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--flash", action="store_true", default=True)
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "branch_head.json").write_text(
        json.dumps({"branch": "diag/thermometer2-aether-full-vanilla", "head": head}, indent=2),
        encoding="utf-8",
    )

    ssid, pwd = load_wifi()
    if args.build_only:
        build_esp(
            service_uid="00000000-0000-0000-0000-000000000001",
            ssid=ssid,
            password=pwd,
            smoke=args.smoke,
            port="COM5",
            flash=False,
        )
        print(f"FIRMWARE_SHA256={firmware_sha256()}", flush=True)
        return 0

    port = find_com()
    exe = build_receiver()
    rx_log = RESULTS / "receiver.log"
    session = HERE / "thermometer2_full_vanilla_rx_session"
    proc = start_receiver(exe, session)
    try:
        uid = wait_uid(rx_log)
        print(f"RECEIVER_UID={uid}", flush=True)
        build_esp(service_uid=uid, ssid=ssid, password=pwd, smoke=args.smoke, port=port, flash=args.flash)
        print(f"FIRMWARE_SHA256={firmware_sha256()}", flush=True)
        esp_log = RESULTS / ("smoke_esp.log" if args.smoke else "measure_esp.log")
        capture(port, 600 if args.smoke else 7200, esp_log)
        if args.smoke:
            res = parse_smoke(esp_log, rx_log)
            out = RESULTS / "smoke_result.json"
            out.write_text(json.dumps(res, indent=2), encoding="utf-8")
            print(json.dumps(res, indent=2), flush=True)
            ok = res["full_rx_count"] >= 3 and not res["panic"]
            return 0 if ok else 1
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
