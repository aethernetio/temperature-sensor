#!/usr/bin/env python3
"""Enable PPK2 source meter.

Short-lived scripts used to call toggle ON and exit — but PPK2_API.__del__
sends RESET and DUT power drops (LED red→green). By default this script
starts a detached hold process that keeps the serial session alive.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from ppk2_api.ppk2_api import PPK2_API

HERE = Path(__file__).resolve().parent
HOLD = HERE / "ppk2_hold_power.py"
PID_FILE = HERE / "ppk2_hold.pid"
# Prefer a real interpreter (venv Scripts/python.exe may re-exec and double-run).
IDF_PY = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
SITE = HERE / "ppk2-venv" / "Lib" / "site-packages"


def hold_python() -> Path:
    if IDF_PY.exists():
        return IDF_PY
    return Path(sys.executable)


def stop_hold() -> None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="ascii").strip())
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                os.kill(pid, 9)
        except Exception:
            pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    # also kill by command line match (Windows)
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'ppk2_hold_power' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            capture_output=True,
            check=False,
        )
    time.sleep(0.5)


def start_hold(mv: int) -> None:
    stop_hold()
    log = open(HERE / "ppk2_hold.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SITE) + os.pathsep + env.get("PYTHONPATH", "")
    env["PPK_HOLD_CHILD"] = "1"
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    subprocess.Popen(
        [str(hold_python()), "-B", str(HOLD), f"--voltage-mv={mv}"],
        cwd=str(HERE.parent),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=creationflags,
        close_fds=True,
    )
    # wait until pid file appears
    for _ in range(50):
        if PID_FILE.exists():
            print(f"HOLD_PID={PID_FILE.read_text(encoding='ascii').strip()}")
            return
        time.sleep(0.1)
    print("HOLD_STARTED_NO_PID_YET")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voltage-mv", type=int, default=3000)
    ap.add_argument("--settle-seconds", type=float, default=2.0)
    ap.add_argument("--off", action="store_true", help="Turn DUT power OFF")
    ap.add_argument(
        "--no-hold",
        action="store_true",
        help="One-shot ON without hold process (power will drop on exit)",
    )
    args = ap.parse_args()

    if args.off:
        stop_hold()
        devices = PPK2_API.list_devices()
        print("devices=", devices)
        if not devices:
            print("No PPK2 found after stop_hold")
            return 1
        ppk = PPK2_API(devices[0][0])
        # avoid RESET in __del__ flipping state oddly; set OFF explicitly
        ppk.__del__ = lambda *a, **k: None  # type: ignore[method-assign]
        ppk.get_modifiers()
        ppk.set_source_voltage(args.voltage_mv)
        ppk.use_source_meter()
        ppk.toggle_DUT_power("OFF")
        print("DUT power OFF")
        ppk.ser.close()
        return 0

    if args.no_hold:
        devices = PPK2_API.list_devices()
        print("devices=", devices)
        if not devices:
            raise SystemExit("No PPK2 found")
        ppk = PPK2_API(devices[0][0])
        ppk.get_modifiers()
        ppk.set_source_voltage(args.voltage_mv)
        ppk.use_source_meter()
        ppk.toggle_DUT_power("ON")
        print(f"DUT power ON at {args.voltage_mv} mV (no-hold; will drop on exit)")
        return 0

    print(f"Starting hold process at {args.voltage_mv} mV (keeps LED red)")
    start_hold(args.voltage_mv)
    time.sleep(max(args.settle_seconds, 1.0))
    print(f"DUT power ON at {args.voltage_mv} mV")
    print("LEFT_POWERED=1 HOLD=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
