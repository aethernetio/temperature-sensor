#!/usr/bin/env python3
"""One-shot PPK power-cycle + Espressif COM flash (no numpy)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUILD = ROOT / "build-esp-ordinary-full-ready-control"
PPK_SITE = HERE.parent / "ppk2-venv" / "Lib" / "site-packages"
IDF_PYTHON = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
ESPRESSIF_VID = 0x303A
VOLTAGE_MV = 3000


def first_espressif_port() -> str | None:
    import serial.tools.list_ports as lp

    for p in lp.comports():
        if p.vid == ESPRESSIF_VID:
            print(
                f"found {p.device} VID={p.vid:04X} PID={p.pid:04X} {p.description}",
                flush=True,
            )
            return p.device
    return None


def wait_espressif(timeout_s: float) -> str | None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        port = first_espressif_port()
        if port:
            return port
        time.sleep(0.15)
    return None


def ppk_power_cycle() -> None:
    sys.path.insert(0, str(PPK_SITE))
    from ppk2_api.ppk2_api import PPK2_API

    devices = PPK2_API.list_devices()
    if not devices:
        raise RuntimeError("No PPK2 found")
    port = devices[0][0]
    print(f"PPK2={port}", flush=True)
    ppk = PPK2_API(port)
    ppk.__del__ = lambda *a, **k: None  # type: ignore[method-assign]
    ppk.set_source_voltage(VOLTAGE_MV)
    ppk.use_source_meter()
    ppk.toggle_DUT_power("OFF")
    print("DUT_POWER=OFF", flush=True)
    time.sleep(2.0)
    ppk.toggle_DUT_power("ON")
    print(f"DUT_POWER=ON mv={VOLTAGE_MV}", flush=True)
    time.sleep(0.5)


def flash_port(port: str) -> None:
    import serial

    flash_args = BUILD / "flash_args"
    if not flash_args.is_file():
        raise RuntimeError(f"missing {flash_args}")
    for open_try in range(6):
        try:
            ser = serial.Serial(port, 115200, timeout=0.2)
            ser.setDTR(False)
            ser.setRTS(True)
            time.sleep(0.05)
            ser.close()
            break
        except Exception as e:
            print(f"reset_hold_try={open_try} {e}", flush=True)
            time.sleep(0.2)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    last_rc = 1
    for baud in (460800, 115200):
        cmd = [
            str(IDF_PYTHON),
            "-m",
            "esptool",
            "--chip",
            "esp32c6",
            "-p",
            port,
            "-b",
            str(baud),
            "--before",
            "default-reset",
            "--after",
            "hard-reset",
            "write-flash",
            "@flash_args",
        ]
        print("+", " ".join(cmd), flush=True)
        r = subprocess.run(cmd, cwd=str(BUILD), env=env)
        last_rc = r.returncode
        if r.returncode == 0:
            return
        time.sleep(0.3)
    raise RuntimeError(f"esptool failed rc={last_rc}")


def main() -> int:
    last = "no attempt"
    for attempt in range(1, 9):
        print(f"=== FLASH attempt {attempt}/8 ===", flush=True)
        ppk_power_cycle()
        t0 = time.time()
        port: str | None = None
        while time.time() - t0 < 50.0:
            port = first_espressif_port()
            if port:
                try:
                    flash_port(port)
                    print(f"FLASH_OK port={port}", flush=True)
                    return 0
                except Exception as e:
                    last = str(e)
                    print(f"flash_fail={e}", flush=True)
                    break
            time.sleep(0.12)
        if not port:
            last = "NO_ESPRESSIF_COM"
        time.sleep(0.8)
    print(f"FLASH_FAIL {last}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
