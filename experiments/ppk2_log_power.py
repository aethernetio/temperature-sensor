#!/usr/bin/env python3
"""Keep PPK2 DUT ON and log current samples to CSV (replaces plain hold)."""
from __future__ import annotations

import argparse
import atexit
import csv
import os
import sys
import time
from pathlib import Path

from ppk2_api.ppk2_api import PPK2_API

HERE = Path(__file__).resolve().parent
PID_FILE = HERE / "ppk2_hold.pid"


def disable_reset_on_del(ppk: PPK2_API) -> None:
    ppk.__del__ = lambda *a, **k: None  # type: ignore[method-assign]


def apply_on(ppk: PPK2_API, mv: int) -> None:
    ppk.set_source_voltage(mv)
    ppk.use_source_meter()
    ppk.toggle_DUT_power("ON")


def acquire_singleton() -> object | None:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, True, "Local\\AetherPPK2HoldPower")
        if kernel32.GetLastError() == 183:
            if mutex:
                kernel32.CloseHandle(mutex)
            return None
        return mutex
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voltage-mv", type=int, default=3000)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--duration-sec", type=float, default=0.0, help="0=forever")
    ap.add_argument("--flush-every", type=int, default=500)
    args = ap.parse_args()

    lock = acquire_singleton()
    if lock is None:
        print("another PPK hold owns the device; stop it first", flush=True)
        return 3

    devices = PPK2_API.list_devices()
    if not devices:
        print("No PPK2", flush=True)
        return 2
    port = devices[0][0]
    print(f"log start port={port} mv={args.voltage_mv} out={args.out}", flush=True)

    ppk = PPK2_API(port)
    disable_reset_on_del(ppk)
    ppk.get_modifiers()
    apply_on(ppk, args.voltage_mv)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    f = args.out.open("w", newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(["t_s", "uA"])
    n = 0
    t0 = time.time()
    sum_uA = 0.0
    min_uA = 1e300
    max_uA = -1e300

    def on_exit() -> None:
        try:
            f.flush()
            f.close()
        except Exception:
            pass
        try:
            if ppk.ser and ppk.ser.is_open:
                ppk.ser.close()
        except Exception:
            pass
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(on_exit)

    ppk.start_measuring()
    deadline = t0 + args.duration_sec if args.duration_sec > 0 else None
    last_status = t0
    try:
        while True:
            if deadline and time.time() >= deadline:
                break
            # keep DUT asserted periodically
            if int(time.time() - t0) % 30 == 0:
                apply_on(ppk, args.voltage_mv)
            raw = ppk.get_data()
            now = time.time()
            if raw:
                chunk, _ = ppk.get_samples(raw)
                for uA in chunk:
                    w.writerow([f"{now - t0:.6f}", f"{uA:.3f}"])
                    n += 1
                    sum_uA += uA
                    if uA < min_uA:
                        min_uA = uA
                    if uA > max_uA:
                        max_uA = uA
                    if n % args.flush_every == 0:
                        f.flush()
            if now - last_status >= 5.0:
                avg = (sum_uA / n) if n else 0.0
                print(
                    f"samples={n} avg_mA={avg/1000:.3f} min_mA={min_uA/1000:.3f} "
                    f"max_mA={max_uA/1000:.3f} elapsed={now-t0:.1f}s",
                    flush=True,
                )
                last_status = now
            else:
                time.sleep(0.001)
    finally:
        try:
            ppk.stop_measuring()
        except Exception:
            pass
        avg = (sum_uA / n) if n else 0.0
        print(
            f"DONE samples={n} avg_mA={avg/1000:.3f} min_mA={min_uA/1000:.3f} "
            f"max_mA={max_uA/1000:.3f}",
            flush=True,
        )
        summary = args.out.with_suffix(".summary.txt")
        summary.write_text(
            f"samples={n}\navg_mA={avg/1000:.6f}\nmin_mA={min_uA/1000:.6f}\n"
            f"max_mA={max_uA/1000:.6f}\nvoltage_mv={args.voltage_mv}\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
