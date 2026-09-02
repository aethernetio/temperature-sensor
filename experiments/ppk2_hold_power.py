#!/usr/bin/env python3
"""Keep PPK2 DUT power ON.

ppk2_api.PPK2_API.__del__ sends RESET on object destruction, which turns the
DUT LED green again after short scripts exit. This process holds the serial
session open indefinitely and re-asserts ON periodically.
"""
from __future__ import annotations

import argparse
import atexit
import os
import sys
import time
from pathlib import Path

from ppk2_api.ppk2_api import PPK2_API

PID_FILE = Path(__file__).resolve().parent / "ppk2_hold.pid"
LOCK_FILE = Path(__file__).resolve().parent / "ppk2_hold.lock"
LOG_FILE = Path(__file__).resolve().parent / "ppk2_hold.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def disable_reset_on_del(ppk: PPK2_API) -> None:
    # Prevent RESET when interpreter tears the object down unexpectedly.
    ppk.__del__ = lambda *a, **k: None  # type: ignore[method-assign]


def apply_on(ppk: PPK2_API, mv: int) -> None:
    ppk.set_source_voltage(mv)
    ppk.use_source_meter()
    ppk.toggle_DUT_power("ON")


def acquire_singleton() -> object | None:
    """Return a live handle if we are the only hold instance; else None."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, True, "Local\\AetherPPK2HoldPower")
        last = kernel32.GetLastError()
        # ERROR_ALREADY_EXISTS = 183
        if last == 183:
            if mutex:
                kernel32.CloseHandle(mutex)
            return None
        return mutex
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd
    except FileExistsError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voltage-mv", type=int, default=3000)
    args = ap.parse_args()

    lock = acquire_singleton()
    if lock is None:
        return 0

    devices = PPK2_API.list_devices()
    if not devices:
        log("No PPK2 found")
        return 2
    port = devices[0][0]
    log(f"hold start port={port} voltage_mv={args.voltage_mv} pid={os.getpid()}")

    ppk = PPK2_API(port)
    disable_reset_on_del(ppk)
    ppk.get_modifiers()
    apply_on(ppk, args.voltage_mv)
    log("DUT ON held (LED should stay red)")

    PID_FILE.write_text(str(os.getpid()), encoding="ascii")

    def on_exit() -> None:
        try:
            if ppk.ser and ppk.ser.is_open:
                ppk.ser.close()
        except Exception:
            pass
        try:
            if sys.platform == "win32" and not isinstance(lock, int):
                import ctypes

                ctypes.windll.kernel32.CloseHandle(lock)
            elif isinstance(lock, int):
                os.close(lock)
                LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(on_exit)

    while True:
        try:
            apply_on(ppk, args.voltage_mv)
            ppk.start_measuring()
            t0 = time.time()
            while time.time() - t0 < 1.0:
                raw = ppk.get_data()
                if raw:
                    ppk.get_samples(raw)
                time.sleep(0.01)
            ppk.stop_measuring()
        except Exception as e:
            log(f"reopen after error: {e!r}")
            try:
                if ppk.ser:
                    ppk.ser.close()
            except Exception:
                pass
            time.sleep(1.0)
            devices = PPK2_API.list_devices()
            if not devices:
                time.sleep(2.0)
                continue
            ppk = PPK2_API(devices[0][0])
            disable_reset_on_del(ppk)
            try:
                ppk.get_modifiers()
            except Exception:
                pass
            apply_on(ppk, args.voltage_mv)
            log("DUT ON re-applied after reconnect")
        time.sleep(5.0)


if __name__ == "__main__":
    raise SystemExit(main())
