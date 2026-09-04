#!/usr/bin/env python3
"""PPK2 online current integration for long runs (no full-rate CSV).

Integrates DUT current continuously and checkpoints accumulated charge/energy
every N seconds so a host crash does not lose the whole measurement.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path

from ppk2_api.ppk2_api import PPK2_API

HERE = Path(__file__).resolve().parent
PID_FILE = HERE / "ppk2_integrate.pid"

# PPK2 default sample interval when measuring at 100 kHz.
DEFAULT_DT_US = 10.0
WAKE_UA = 2500.0
SLEEP_UA = 150.0
WAKE_CONFIRM_SAMPLES = 400  # 4 ms @ 100 kHz
SLEEP_CONFIRM_SAMPLES = 30000  # 300 ms @ 100 kHz
MAX_SEGMENTS = 64


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
        mutex = kernel32.CreateMutexW(None, True, "Local\\AetherPPK2IntegrateRun")
        if kernel32.GetLastError() == 183:
            if mutex:
                kernel32.CloseHandle(mutex)
            return None
        return mutex
    return True


def integrate_samples(
    chunk: list[float], dt_us: float, voltage_v: float, state: dict
) -> None:
    for uA in chunk:
        state["sample_count"] += 1
        state["charge_uC"] += uA * dt_us / 1_000_000.0
        state["energy_J"] += voltage_v * uA * dt_us / 1_000_000_000_000.0
        state["sum_uA"] += uA
        if uA < state["min_uA"]:
            state["min_uA"] = uA
        if uA > state["max_uA"]:
            state["max_uA"] = uA
        update_wake_sleep(uA, dt_us, state)


def update_wake_sleep(uA: float, dt_us: float, state: dict) -> None:
    segs = state["segments"]
    t_s = state["sample_count"] * dt_us / 1_000_000.0
    if not state["in_wake"]:
        if uA >= WAKE_UA:
            state["wake_run"] += 1
        else:
            state["wake_run"] = 0
        if state["wake_run"] >= WAKE_CONFIRM_SAMPLES:
            state["in_wake"] = True
            state["wake_run"] = 0
            state["sleep_run"] = 0
            segs.append(
                {
                    "kind": "wake",
                    "t_s": round(t_s, 3),
                    "energy_J": state["energy_J"],
                    "charge_uC": state["charge_uC"],
                }
            )
            if len(segs) > MAX_SEGMENTS:
                del segs[: len(segs) - MAX_SEGMENTS]
        return
    if uA <= SLEEP_UA:
        state["sleep_run"] += 1
    else:
        state["sleep_run"] = 0
    if state["sleep_run"] >= SLEEP_CONFIRM_SAMPLES:
        state["in_wake"] = False
        state["sleep_run"] = 0
        segs.append(
            {
                "kind": "sleep",
                "t_s": round(t_s, 3),
                "energy_J": state["energy_J"],
                "charge_uC": state["charge_uC"],
            }
        )
        if len(segs) > MAX_SEGMENTS:
            del segs[: len(segs) - MAX_SEGMENTS]


def snapshot(state: dict, t0: float, voltage_mv: int) -> dict:
    elapsed = time.time() - t0
    charge_mAh = state["charge_uC"] / 3600.0 / 1000.0
    charge_mC = state["charge_uC"] / 1000.0
    avg_mA = (charge_mAh / (elapsed / 3600.0)) if elapsed > 0 else 0.0
    avg_uA = (state["sum_uA"] / state["sample_count"]) if state["sample_count"] else 0.0
    return {
        "start_timestamp": state["start_iso"],
        "end_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(elapsed, 3),
        "sample_count": state["sample_count"],
        "voltage_mv": voltage_mv,
        "charge_uC": state["charge_uC"],
        "charge_mC": round(charge_mC, 6),
        "charge_mAh": round(charge_mAh, 9),
        "energy_J": round(state["energy_J"], 9),
        "avg_current_mA": round(avg_mA, 6),
        "avg_uA": round(avg_uA, 3),
        "min_uA": state["min_uA"] if state["min_uA"] < 1e299 else 0.0,
        "max_uA": state["max_uA"] if state["max_uA"] > -1e299 else 0.0,
        "segments": list(state.get("segments") or []),
        "in_wake": bool(state.get("in_wake", False)),
    }


def write_checkpoint(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voltage-mv", type=int, default=3000)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--checkpoint-every-sec", type=float, default=10.0)
    ap.add_argument("--decimated-out", type=Path, default=None)
    ap.add_argument("--decimate-every", type=int, default=10000)
    ap.add_argument("--duration-sec", type=float, default=0.0, help="0=until SIGTERM")
    ap.add_argument("--dt-us", type=float, default=DEFAULT_DT_US)
    args = ap.parse_args()

    lock = acquire_singleton()
    if lock is None:
        print("another PPK integrate owns the device", flush=True)
        return 3

    devices = PPK2_API.list_devices()
    if not devices:
        print("No PPK2", flush=True)
        return 2
    port = devices[0][0]
    voltage_v = args.voltage_mv / 1000.0
    print(
        f"integrate start port={port} mv={args.voltage_mv} "
        f"checkpoint={args.checkpoint}",
        flush=True,
    )

    ppk = PPK2_API(port)
    disable_reset_on_del(ppk)
    ppk.get_modifiers()
    apply_on(ppk, args.voltage_mv)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")

    state = {
        "start_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sample_count": 0,
        "charge_uC": 0.0,
        "energy_J": 0.0,
        "sum_uA": 0.0,
        "min_uA": 1e300,
        "max_uA": -1e300,
        "in_wake": False,
        "wake_run": 0,
        "sleep_run": 0,
        "segments": [],
    }
    t0 = time.time()
    last_ckpt = t0
    stop = False

    def on_stop(*_a: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    dec_f = None
    if args.decimated_out is not None:
        args.decimated_out.parent.mkdir(parents=True, exist_ok=True)
        dec_f = args.decimated_out.open("w", encoding="utf-8")
        dec_f.write("t_s,uA\n")

    def on_exit() -> None:
        try:
            if dec_f is not None:
                dec_f.flush()
                dec_f.close()
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
    try:
        while not stop:
            if deadline and time.time() >= deadline:
                break
            if int(time.time() - t0) % 30 == 0:
                apply_on(ppk, args.voltage_mv)
            raw = ppk.get_data()
            if raw:
                chunk, _ = ppk.get_samples(raw)
                integrate_samples(chunk, args.dt_us, voltage_v, state)
                if dec_f is not None and state["sample_count"] % args.decimate_every == 0:
                    elapsed = time.time() - t0
                    avg = state["sum_uA"] / state["sample_count"]
                    dec_f.write(f"{elapsed:.3f},{avg:.3f}\n")
            now = time.time()
            if now - last_ckpt >= args.checkpoint_every_sec:
                write_checkpoint(args.checkpoint, snapshot(state, t0, args.voltage_mv))
                last_ckpt = now
            else:
                time.sleep(0.001)
    finally:
        try:
            ppk.stop_measuring()
        except Exception:
            pass
        final = snapshot(state, t0, args.voltage_mv)
        final["status"] = "complete"
        write_checkpoint(args.checkpoint, final)
        print(
            f"DONE elapsed={final['elapsed_s']}s energy_J={final['energy_J']} "
            f"charge_mAh={final['charge_mAh']} avg_mA={final['avg_current_mA']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
