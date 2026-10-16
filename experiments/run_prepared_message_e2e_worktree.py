#!/usr/bin/env python3
"""Historical prepared-message E2E from restore worktree (no PPK capture)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
AETHER_LOCAL = Path(r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0")
RX_ROOT = HERE / "prepared_message_receiver"
RX_BUILD = RX_ROOT / "build-msvc"
RX_SESSION = HERE / "prepared_message_rx_session_restore"
ESP_BUILD = ROOT / "build-esp32c6-save-bench-smoke"
ESP_LOG = HERE / "prepared_message_e2e_esp_restore.log"
RX_LOG = HERE / "prepared_message_e2e_receiver_restore.log"
UID_FILE = HERE / "prepared_message_receiver_uid_restore.txt"
RESULT_JSON = HERE / "prepared_message_e2e_restore_result.json"

ESPRESSIF_VID = 0x303A
BENCH_CLIENT_ID = "prepared_message_bench_v6"
CAPTURE_SEC = 600


def load_wifi() -> tuple[str, str]:
    for path in (
        HERE / "local_aether_wifi.env",
        ROOT.parent / "temperature-sensor-prepared/experiments/local_aether_wifi.env",
    ):
        if not path.is_file():
            continue
        env: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
        ssid = env.get("AETHER_WIFI_SSID", "")
        pwd = env.get("AETHER_WIFI_PASSWORD", "")
        if ssid and pwd:
            return ssid, pwd
    ssid = os.environ.get("AETHER_WIFI_SSID", "").strip()
    pwd = os.environ.get("AETHER_WIFI_PASSWORD", "").strip()
    if ssid and pwd:
        return ssid, pwd
    raise RuntimeError("Set AETHER_WIFI_SSID/PASSWORD or local_aether_wifi.env")


def find_espressif_com() -> str:
    try:
        import serial.tools.list_ports  # type: ignore
    except ImportError:
        raise RuntimeError("pip install pyserial") from None
    for p in serial.tools.list_ports.comports():
        if p.vid == ESPRESSIF_VID and p.device:
            return p.device
    raise RuntimeError("No Espressif COM (VID 0x303A)")


IDF_PATH = Path(os.environ.get("IDF_PATH", r"C:\Espressif\frameworks\esp-idf-v6.0.2"))
EXPORT_PS1 = IDF_PATH / "export.ps1"


def idf_ps(extra: str, cwd: Path | None = None) -> list[str]:
    root = (cwd or ROOT).as_posix()
    ps = f"""
$ErrorActionPreference = 'Stop'
$env:IDF_PATH = '{IDF_PATH.as_posix()}'
$env:PYTHONPATH = $null
$env:ESP_IDF_EXPORT_DEBUG = $null
. '{EXPORT_PS1.as_posix()}'
Set-Location '{root}'
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


def run_ps(script: str, cwd: Path) -> None:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        cwd=str(cwd),
        env=os.environ.copy(),
    )
    if r.returncode != 0:
        raise RuntimeError(f"powershell failed code={r.returncode}")


def release_serial_port(port: str) -> None:
    ps = f"""
$port = '{port}'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object {{ $_.CommandLine -match 'idf_monitor' -and $_.CommandLine -match [regex]::Escape($port) }} |
  ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Seconds 1
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=False,
    )


def run_idf(extra: str, cwd: Path | None = None) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(idf_ps(extra, cwd), cwd=str(cwd or ROOT), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"idf step failed code={r.returncode}")


def build_receiver() -> Path:
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    RX_SESSION.mkdir(parents=True, exist_ok=True)
    aether = (
        f"-DCPM_aether-client-cpp_SOURCE={AETHER_LOCAL.as_posix()}"
        if AETHER_LOCAL.is_dir()
        else ""
    )
    rx_s = RX_ROOT.as_posix()
    rx_b = RX_BUILD.as_posix()
    ps = f"""
$ErrorActionPreference='Stop'
$vswhere = "${{env:ProgramFiles(x86)}}\\Microsoft Visual Studio\\Installer\\vswhere.exe"
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvars = Join-Path $vs 'VC\\Auxiliary\\Build\\vcvars64.bat'
cmd /c "`"$vcvars`" && cmake -S {rx_s} -B {rx_b} -G Ninja -DCMAKE_BUILD_TYPE=Release {aether} && cmake --build {rx_b} --target prepared_message_receiver --parallel"
"""
    run_ps(ps, ROOT)
    exe = RX_BUILD / "prepared_message_receiver.exe"
    if not exe.is_file():
        raise RuntimeError(f"missing {exe}")
    return exe


def start_receiver(exe: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["AE_RECEIVER_SESSION_DIR"] = str(RX_SESSION)
    if RX_SESSION.exists():
        for f in RX_SESSION.glob("*"):
            if f.is_file():
                f.unlink()
    out = open(RX_LOG, "w", encoding="utf-8", errors="replace")
    err = open(str(RX_LOG) + ".err", "w", encoding="utf-8", errors="replace")
    return subprocess.Popen([str(exe)], stdout=out, stderr=err, env=env)


def wait_receiver_uid(timeout_s: float = 600.0) -> str:
    deadline = time.time() + timeout_s
    pat = re.compile(r"RECEIVER_UID=([0-9a-fA-F\-]{36})")
    while time.time() < deadline:
        if RX_LOG.is_file():
            m = pat.search(RX_LOG.read_text(encoding="utf-8", errors="replace"))
            if m:
                uid = m.group(1)
                UID_FILE.write_text(uid, encoding="utf-8")
                return uid
        time.sleep(2.0)
    raise RuntimeError("receiver UID timeout")


def build_esp(service_uid: str, ssid: str, password: str, flash: bool, port: str) -> None:
    ESP_BUILD.mkdir(parents=True, exist_ok=True)
    import shutil

    cache = ESP_BUILD / "CMakeCache.txt"
    if ESP_BUILD.is_dir() and not cache.is_file():
        shutil.rmtree(ESP_BUILD)
    elif cache.is_file():
        text = cache.read_text(encoding="utf-8", errors="replace")
        if "IDF_TARGET:STRING=esp32c6" not in text:
            shutil.rmtree(ESP_BUILD)
    ESP_BUILD.mkdir(parents=True, exist_ok=True)
    # ESP firmware uses CPM pin from CMakeLists; do not override aether source here.
    aether_cpm = ""
    b = ESP_BUILD.as_posix()
    pwd_ps = password.replace("'", "''")
    ssid_ps = ssid.replace("'", "''")
    extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd_ps}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG_DEFAULTS=sdkconfig.defaults',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_PREPARED_MESSAGE_E2E=1',
  '-D','AE_EXP_SKIP_DTOR_SAVE=1',
  '-D','AE_EXP_WIFI_LIFECYCLE=',
  '-D','AE_EXP_FULL_CYCLES=',
  '-D','AETHER_PREPARED_NONCE_RESERVE=10'
)
{aether_cpm}if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py @common reconfigure
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
idf.py @common build
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    run_idf(extra)
    if not flash:
        return
    release_serial_port(port)
    flash_extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd_ps}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG_DEFAULTS=sdkconfig.defaults',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_PREPARED_MESSAGE_E2E=1',
  '-D','AE_EXP_SKIP_DTOR_SAVE=1',
  '-D','AE_EXP_WIFI_LIFECYCLE=',
  '-D','AE_EXP_FULL_CYCLES=',
  '-D','AETHER_PREPARED_NONCE_RESERVE=10'
)
{aether_cpm}idf.py @common -p {port} erase-flash
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
idf.py @common -p {port} flash
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    run_idf(flash_extra)


def capture_serial(port: str, seconds: int) -> None:
    elf = (ESP_BUILD / "temperature_sensor.elf").as_posix()
    mon_extra = f"""
python $env:IDF_PATH/tools/idf_monitor.py -p {port} -b 115200 --print_filter *:I {elf}
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    mon = subprocess.Popen(
        idf_ps(mon_extra),
        stdout=open(ESP_LOG, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        env=env,
    )
    try:
        end = time.time() + seconds
        while time.time() < end:
            if "PREPARED_E2E_DONE" in ESP_LOG.read_text(encoding="utf-8", errors="replace"):
                time.sleep(5)
                break
            time.sleep(3)
    finally:
        mon.terminate()
        try:
            mon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mon.kill()


def parse_results() -> dict:
    rx_text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.is_file() else ""
    esp_text = ESP_LOG.read_text(encoding="utf-8", errors="replace") if ESP_LOG.is_file() else ""
    full_m = re.search(r"full=(\d+)/1", rx_text)
    prep_m = re.search(r"prepared=(\d+)/10", rx_text)
    dup_m = re.search(r"duplicates=(\d+)", rx_text)
    heap_panic = "assert failed" in esp_text or "Guru Meditation" in esp_text
    return {
        "full_rx": int(full_m.group(1)) if full_m else 0,
        "prepared_rx": int(prep_m.group(1)) if prep_m else 0,
        "duplicates": int(dup_m.group(1)) if dup_m else -1,
        "heap_panic": heap_panic,
        "esp_done": "PREPARED_E2E_DONE" in esp_text,
        "receiver_uid": UID_FILE.read_text(encoding="utf-8").strip() if UID_FILE.is_file() else "",
    }


def main() -> int:
    ssid, password = load_wifi()
    port = find_espressif_com()
    print(f"ESP_PORT={port} SSID={ssid}", flush=True)
    exe = build_receiver()
    proc = start_receiver(exe)
    try:
        uid = wait_receiver_uid()
        print(f"SERVICE_UID={uid}", flush=True)
        build_esp(uid, ssid, password, flash=True, port=port)
        capture_serial(port, CAPTURE_SEC)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    res = parse_results()
    import json

    res["pass"] = (
        res["full_rx"] == 1
        and res["prepared_rx"] >= 8
        and not res["heap_panic"]
        and res["esp_done"]
    )
    RESULT_JSON.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2), flush=True)
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
