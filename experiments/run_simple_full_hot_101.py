#!/usr/bin/env python3
"""One run: 1× FULL vanilla + 100× HOT (2 s sleep). No PPK capture."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PREPARED = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
BUILD = ROOT / "build-simple-full-hot-101"
RESULTS = HERE / "simple_full_hot_101_results"
AETHER = ROOT / "deps" / "aether-client-cpp-157aadbe-diag"
AETHER_PATCH = HERE / "patches" / "aether-157aadbe-full-vanilla-stage1.patch"
USER_CONFIG = ROOT / "main" / "user_config_full_vanilla.h"
RX_SRC = PREPARED / "temperature_receiver"
RX_BUILD = HERE / "simple_full_hot_101_rx_build"
BENCH_CLIENT_ID = "reliability_full_v1"
VOLTAGE_MV = 3000

FULL_ACTIVE_TIMEOUT_S = 120
HOT_ACTIVE_TIMEOUT_S = 45
SLEEP_S = 2
HOT_COUNT = 100
TAIL_DELIVERY_S = 30

_VENV_PY = PREPARED / "experiments" / "ppk2-venv" / "Scripts" / "python.exe"
if _VENV_PY.is_file() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PREPARED / "experiments"))
import run_thermometer2_sleep_bisect as sb  # noqa: E402
import run_thermometer2_aether_full_vanilla as vanilla  # noqa: E402

RE_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_MSG = re.compile(
    r"MSG_NUM=(\d+)\s+sender=([0-9a-f-]{36})", re.I
)


def utc_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
    ).strip()


def git_status_porcelain() -> str:
    return subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=str(ROOT), text=True
    )


def stop_experiment_holders() -> None:
    sb.stop_external_ppk_holders()
    if sys.platform != "win32":
        return
    ps = r"""
Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -and (
      $_.CommandLine -match 'ppk2_hold_power|ppk2_log_power|ppk2_integrate_run|ppk2_power\.py' -or
      ($_.CommandLine -match 'idf_monitor|esp_idf_monitor|esptool' -and
       $_.CommandLine -match 'build-simple-full-hot-101')
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


def recover_esp_port(ppk: sb.PpkSession) -> str | None:
    for i in range(3):
        ppk.set_off()
        time.sleep(4.0)
        ppk.set_on()
        port = sb.wait_espressif_com(timeout_s=60.0)
        if port:
            stop_experiment_holders()
            time.sleep(0.8)
            chip = vanilla.verify_esptool_chip(port)
            if chip.get("ok"):
                return port
        time.sleep(2.0)
    return None


def build_receiver() -> tuple[Path, str]:
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    stop_experiment_holders()
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process aether_hot_tcp_receiver -ErrorAction SilentlyContinue | Stop-Process -Force",
            ],
            capture_output=True,
            check=False,
        )
        time.sleep(1.5)
    exe = RX_BUILD / "aether_hot_tcp_receiver.exe"
    ps = (
        "$ErrorActionPreference='Stop'\n"
        '$vswhere = "${env:ProgramFiles(x86)}\\Microsoft Visual Studio\\Installer\\vswhere.exe"\n'
        "$vs = & $vswhere -latest -products * -requires "
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath\n"
        "$vcvars = Join-Path $vs 'VC\\Auxiliary\\Build\\vcvars64.bat'\n"
        f'cmd /c "`"$vcvars`" && cmake -S `"{RX_SRC.as_posix()}`" -B '
        f'`"{RX_BUILD.as_posix()}`" -G Ninja -DCMAKE_BUILD_TYPE=Release '
        f"-DCPM_aether-client-cpp_SOURCE=`\"{AETHER.as_posix()}`\" && cmake --build "
        f'`"{RX_BUILD.as_posix()}`" --target aether_hot_tcp_receiver --parallel"\n'
    )
    subprocess.check_call(["powershell", "-NoProfile", "-Command", ps], cwd=str(ROOT))
    if not exe.is_file():
        exe = RX_BUILD / "Release" / "aether_hot_tcp_receiver.exe"
    return exe, file_sha256(exe)


def start_receiver(exe: Path, log_path: Path, session: Path) -> subprocess.Popen:
    session.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [str(exe), "--session", str(session), "--require-tcp", "1"],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(RX_BUILD),
    )


def wait_uid(log_path: Path, timeout_s: float = 90.0) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if log_path.is_file():
            m = RE_UID.search(log_path.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
        time.sleep(0.5)
    raise RuntimeError("RECEIVER_UID timeout")


def build_esp(*, service_uid: str, ssid: str, pwd: str) -> str:
    b = BUILD.as_posix()
    sdk = (
        f"{ROOT.as_posix()}/sdkconfig.defaults;"
        f"{ROOT.as_posix()}/sdkconfig.defaults.thermometer2_aether_full_vanilla"
    )
    sdkconfig = f"{b}/sdkconfig"
    ssid_ps = ssid.replace("'", "''")
    pwd_ps = pwd.replace("'", "''")
    aether = AETHER.as_posix()
    ucfg = USER_CONFIG.as_posix()
    extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd_ps}'
$env:SDKCONFIG = '{sdkconfig}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG={sdkconfig}',
  '-D','SDKCONFIG_DEFAULTS={sdk}',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_SIMPLE_FULL_HOT_101=1',
  '-D','BOARD=0',
  '-D','AE_EXP_WIFI_CANONICAL=1',
  '-D','CPM_aether-client-cpp_SOURCE={aether}',
  '-D','USER_CONFIG={ucfg}'
)
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
}}
idf.py @common build
"""
    vanilla.ensure_aether_patched()
    vanilla.run_idf(extra)
    bin_path = BUILD / "temperature_sensor.bin"
    if not bin_path.is_file():
        raise RuntimeError("firmware bin missing after build")
    return file_sha256(bin_path)


def flash_firmware(port: str) -> None:
    b = BUILD.as_posix()
    extra = f"idf.py -B '{b}' -p {port} flash"
    vanilla.run_idf(extra)


def campaign_deadline_s() -> float:
    return (
        FULL_ACTIVE_TIMEOUT_S
        + HOT_COUNT * (HOT_ACTIVE_TIMEOUT_S + SLEEP_S)
        + TAIL_DELIVERY_S
        + 60
    )


def parse_log(log_path: Path, *, test_sender: str | None) -> tuple[list[int], str | None]:
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    nums: list[int] = []
    sender = test_sender
    for m in RE_MSG.finditer(text):
        n = int(m.group(1))
        s = m.group(2).lower()
        if sender is None:
            sender = s
        if s == sender:
            nums.append(n)
    return nums, sender


def evaluate(nums: list[int]) -> dict:
    c = Counter(nums)
    expect = set(range(1, 102))
    got = set(c.keys())
    missing = sorted(expect - got)
    dups = sorted(n for n, k in c.items() if k > 1)
    full_rx = 1 if c.get(1, 0) >= 1 else 0
    hot_nums = [n for n in nums if n >= 2]
    hot_unique = len({n for n in hot_nums})
    pass_ok = (
        got == expect
        and len(nums) == 101
        and full_rx == 1
        and hot_unique == 100
        and not dups
    )
    return {
        "FULL_RX": f"{full_rx}/1",
        "HOT_RX": f"{hot_unique}/100",
        "missing_numbers": missing,
        "duplicates": dups,
        "msg_rx_count": len(nums),
        "pass": pass_ok,
    }


def write_result(path: Path, payload: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    run_id = utc_tag()
    log_path = RESULTS / f"receiver_{run_id}.log"
    result_path = RESULTS / f"result_{run_id}.json"
    session = HERE / f"simple_full_hot_101_rx_session_{run_id}"

    aether_meta = vanilla.ensure_aether_patched()
    head = git_head()
    local_changes = git_status_porcelain().strip()

    base = {
        "run_id": run_id,
        "git_head": head,
        "git_local_changes": local_changes or "(clean)",
        "aether_meta": aether_meta,
        "SERVICE_UID": None,
        "receiver_sha256": None,
        "firmware_sha256": None,
        "FLASH": "NOT_RUN",
        "TEST": "NOT_RUN",
        "USB_ACCESS": None,
        "logs": {"receiver": str(log_path.relative_to(ROOT))},
        "result_json": str(result_path.relative_to(ROOT)),
    }

    ppk: sb.PpkSession | None = None
    try:
        ssid, pwd = vanilla.load_wifi()
        rx_exe, rx_sha = build_receiver()
        base["receiver_sha256"] = rx_sha
        proc = start_receiver(rx_exe, log_path, session)
        uid = wait_uid(log_path)
        base["SERVICE_UID"] = uid
        print(f"RECEIVER_UID={uid}", flush=True)

        stop_experiment_holders()
        vanilla.stop_stage1_serial_holders()
        ppk = vanilla.open_ppk_session()
        ppk.set_on()
        fw_sha = build_esp(service_uid=uid, ssid=ssid, pwd=pwd)
        base["firmware_sha256"] = fw_sha

        port = recover_esp_port(ppk)
        if not port:
            base["USB_ACCESS"] = "BLOCKED"
            base["FLASH"] = "BLOCKED"
            base["TEST"] = "NOT_RUN"
            base["FULL_RX"] = "0/1"
            base["HOT_RX"] = "0/100"
            base["missing_numbers"] = list(range(1, 102))
            base["duplicates"] = []
            base["actual_attempts"] = "UNKNOWN"
            write_result(result_path, base)
            (RESULTS / "LATEST.json").write_text(
                json.dumps(
                    {"latest_run_id": run_id, "result": str(result_path.relative_to(ROOT))},
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(json.dumps(base, indent=2), flush=True)
            return 2

        base["USB_ACCESS"] = "OK"
        flash_firmware(port)
        base["FLASH"] = "PASS"
        ppk.set_on()
        vanilla.cold_boot_dut(ppk, off_s=4.0, wait_com_s=20.0)

        deadline = time.time() + campaign_deadline_s()
        test_sender: str | None = None
        while time.time() < deadline:
            nums, test_sender = parse_log(log_path, test_sender=test_sender)
            ev = evaluate(nums)
            if ev["pass"]:
                break
            time.sleep(2.0)

        time.sleep(TAIL_DELIVERY_S)
        nums, test_sender = parse_log(log_path, test_sender=test_sender)
        ev = evaluate(nums)
        base.update(ev)
        base["test_sender_uid"] = test_sender
        base["actual_attempts"] = "UNKNOWN"
        base["actual_attempts"] = "UNKNOWN"
        base["TEST"] = "PASS" if ev["pass"] else "FAIL"
        write_result(result_path, base)
        (RESULTS / "LATEST.json").write_text(
            json.dumps(
                {"latest_run_id": run_id, "result": str(result_path.relative_to(ROOT))},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps(base, indent=2), flush=True)
        return 0 if ev["pass"] else 1
    except Exception as e:
        base["TEST"] = "FAIL"
        base["error"] = str(e)
        write_result(result_path, base)
        print(json.dumps(base, indent=2), flush=True)
        return 1
    finally:
        if ppk is not None:
            try:
                ppk.set_off()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
