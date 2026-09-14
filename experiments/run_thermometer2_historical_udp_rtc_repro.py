#!/usr/bin/env python3
"""Reproduce historical UDP RTC Wi-Fi energy AS-IS (no BoardPowerDown).

Mode: AETHER_DIAG_UDP_RTC_INDEX
Interval: original 10 s
UDP: 192.168.68.84:9000
PPK: 3000 mV, visual wake→sleep integration (no 1 mA cutoff).
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402
import run_thermometer2_udp_low_power_1min as lp  # noqa: E402
import run_prepared_power_factor_study as pf  # noqa: E402
import _diag_udp_rtc_index_flash as flash_mod  # noqa: E402

BUILD = ROOT / "build-udp-rtc-historical-repro"
OUT_DIR = HERE / "power_factor_results" / "thermometer2_low_energy_plots"
RAW_DIR = HERE / "power_modes_raw" / "thermometer2_historical_udp_rtc"
JSON_OUT = HERE / "power_factor_results" / "thermometer2_historical_udp_rtc_repro.json"
MD_PARTIAL = HERE / "THERMOMETER2_LOW_ENERGY_WIFI_PLUS_SLEEP.md"

UDP_HOST = "192.168.68.84"
UDP_PORT = 9000
VOLTAGE_MV = 3000
MEASURED = 10
# original firmware sleeps 10 s between cycles; allow warmup + 10 measured + margin
CAPTURE_S = 160.0
PERIOD_HINT_S = 10.0


def configure_and_build() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    wifi = pf.camp.APS["chirkov"]
    ssid = wifi["ssid"]
    password = wifi["password"]
    b = BUILD.as_posix()
    defs = (
        f"-B '{b}' "
        f"-D AETHER_DIAG_UDP_RTC_INDEX=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-D AE_UDP_SERVER_HOST={UDP_HOST} "
        f"-D AE_UDP_SERVER_PORT={UDP_PORT}"
    )
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} build
"""
    print("idf.py build (historical UDP RTC)", flush=True)
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"build failed code={r.returncode}")
    sdk = BUILD / "sdkconfig"
    if sdk.exists():
        before = sdk.read_text(encoding="utf-8", errors="replace")
        flash_mod.force_sdk(sdk)
        after = sdk.read_text(encoding="utf-8", errors="replace")
        if before != after:
            print("force_sdk changed sdkconfig; rebuilding", flush=True)
            r2 = sb.run(sb.idf_cmd(f"idf.py {defs} build"), cwd=str(ROOT))
            if r2.returncode != 0:
                raise RuntimeError(f"rebuild after force_sdk failed code={r2.returncode}")


def detect_wakes(
    t: np.ndarray,
    ua: np.ndarray,
    sleep_thr_uA: float = 500.0,
    wake_thr_uA: float = 2000.0,
    min_wake_s: float = 0.05,
    min_sleep_s: float = 0.3,
) -> list[tuple[float, float]]:
    """Find wake intervals: rise above wake_thr until return below sleep_thr
    for min_sleep_s. Ends only when baseline looks like deep sleep."""
    wakes: list[tuple[float, float]] = []
    i = 0
    n = len(t)
    dt = float(np.median(np.diff(t[: min(5000, n - 1)]))) if n > 2 else 1e-4
    min_wake_n = max(3, int(min_wake_s / max(dt, 1e-6)))
    min_sleep_n = max(3, int(min_sleep_s / max(dt, 1e-6)))
    while i < n:
        # find wake start
        while i < n and ua[i] < wake_thr_uA:
            i += 1
        if i >= n:
            break
        # back up to first sample above sleep_thr (visible rise)
        j = i
        while j > 0 and ua[j - 1] > sleep_thr_uA:
            j -= 1
        start = j
        # advance while active; end when sleep_thr sustained
        k = i
        sleep_run = 0
        end = None
        while k < n:
            if ua[k] < sleep_thr_uA:
                sleep_run += 1
                if sleep_run >= min_sleep_n:
                    end = k - sleep_run + 1
                    break
            else:
                sleep_run = 0
            k += 1
        if end is None:
            end = n - 1
        if end - start >= min_wake_n:
            wakes.append((float(t[start]), float(t[end])))
        i = max(end + 1, i + 1)
    return wakes


def integrate_charge_mC(t: np.ndarray, ua: np.ndarray, t0: float, t1: float) -> float:
    m = (t >= t0) & (t <= t1)
    if not np.any(m):
        return 0.0
    tt = t[m]
    ii = ua[m] * 1e-6  # A
    # trapezoid → C, then mC
    if len(tt) < 2:
        return float(ii[0] * (t1 - t0) * 1000.0)
    q_C = float(np.trapezoid(ii, tt))
    return q_C * 1000.0


def save_plots(
    t: np.ndarray,
    ua: np.ndarray,
    wakes: list[tuple[float, float]],
    out_dir: Path,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    overview = out_dir / "historical_10_wakes_overview.png"
    fig, ax = plt.subplots(figsize=(14, 5))
    # downsample for plot
    step = max(1, len(t) // 200000)
    ax.plot(t[::step], ua[::step], lw=0.4, color="C0")
    for i, (a, b) in enumerate(wakes[:12]):
        ax.axvspan(a, b, color="orange", alpha=0.25)
        ax.text((a + b) / 2, ax.get_ylim()[1] * 0.9 if ax.get_ylim()[1] else 1, str(i), ha="center", fontsize=8)
    ax.set_xlabel("t (s)")
    ax.set_ylabel("I (µA)")
    ax.set_title("Historical UDP RTC — all wakes (visual boundaries)")
    ax.set_yscale("symlog", linthresh=100)
    fig.tight_layout()
    fig.savefig(overview, dpi=120)
    plt.close(fig)

    zooms = []
    for i, (a, b) in enumerate(wakes[:10]):
        pad = max(0.15, (b - a) * 0.35)
        m = (t >= a - pad) & (t <= b + pad)
        zpath = out_dir / f"historical_wake_{i:02d}_zoom.png"
        fig, ax = plt.subplots(figsize=(10, 4))
        ts, us = t[m], ua[m]
        step = max(1, len(ts) // 80000)
        ax.plot(ts[::step], us[::step], lw=0.6, color="C0")
        ax.axvline(a, color="green", ls="--", label="wake start")
        ax.axvline(b, color="red", ls="--", label="sleep entry")
        ax.axvspan(a, b, color="orange", alpha=0.2)
        ax.set_xlabel("t (s)")
        ax.set_ylabel("I (µA)")
        ax.set_title(f"Wake {i}: {a:.3f}->{b:.3f}s ({(b-a)*1000:.0f} ms)")
        ax.set_yscale("symlog", linthresh=100)
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()
        fig.savefig(zpath, dpi=120)
        plt.close(fig)
        zooms.append(str(zpath))
    return {"overview": str(overview), "zooms": zooms}


def load_csv(path: Path) -> tuple[np.ndarray, np.ndarray]:
    # may be huge; load with numpy
    data = np.loadtxt(path, delimiter=",", skiprows=1)
    return data[:, 0], data[:, 1]


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-flash", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    branch, sha = sb.git_meta()
    print(f"branch={branch} sha={sha}", flush=True)

    if not args.skip_build:
        print("=== BUILD historical UDP RTC (no BoardPowerDown) ===", flush=True)
        configure_and_build()
    else:
        print("=== SKIP BUILD ===", flush=True)

    lp.kill_udp_9000()
    udp = lp.UdpCollector()

    subprocess.run(
        [
            "powershell",
            "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ppk2_(hold|log|integrate)' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        capture_output=True,
    )
    time.sleep(1)

    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    time.sleep(0.5)

    if not args.skip_flash:
        print("=== FLASH ===", flush=True)
        port = sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=8.0)
        print(f"flashed port={port}", flush=True)
    else:
        print("=== POWER CYCLE (firmware already flashed) ===", flush=True)
        ppk.power_cycle()
        time.sleep(2.0)

    raw_csv = RAW_DIR / f"ppk_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    # Log via subprocess ppk2_log_power while we poll UDP — but we already hold PPK.
    # Stream ourselves into CSV (decimated write every N samples to keep size sane).
    assert ppk.ppk is not None
    ppk.ppk.start_measuring()
    t0 = time.time()
    rows_t: list[float] = []
    rows_u: list[float] = []
    decim = 20  # keep ~5 kHz equivalent from 100 kHz
    buf_i = 0
    last_status = t0
    print(f"=== CAPTURE {CAPTURE_S}s -> {raw_csv} ===", flush=True)
    try:
        while time.time() - t0 < CAPTURE_S:
            udp.poll()
            try:
                raw = ppk.ppk.get_data()
            except OSError as e:
                print(f"ppk_warn={e}", flush=True)
                time.sleep(0.01)
                continue
            if not raw:
                time.sleep(0.001)
                continue
            try:
                chunk, _ = ppk.ppk.get_samples(raw)
            except Exception:
                continue
            now = time.time() - t0
            for s in chunk:
                buf_i += 1
                if buf_i % decim != 0:
                    continue
                rows_t.append(now)
                rows_u.append(float(s))
            if time.time() - last_status > 5:
                print(
                    f"ppk t={now:.1f}s n={len(rows_t)} udp_rx={len(udp.events)}",
                    flush=True,
                )
                last_status = time.time()
    finally:
        try:
            ppk.ppk.stop_measuring()
        except Exception:
            pass
        udp.close()

    # write CSV
    with raw_csv.open("w", encoding="utf-8", newline="") as f:
        f.write("t_s,uA\n")
        for tt, uu in zip(rows_t, rows_u):
            f.write(f"{tt:.6f},{uu:.3f}\n")

    t = np.asarray(rows_t, dtype=float)
    ua = np.asarray(rows_u, dtype=float)
    if len(t) < 100:
        raise SystemExit("PPK capture too short / empty")

    wakes_all = detect_wakes(t, ua)
    print(f"detected_wakes={len(wakes_all)}", flush=True)
    # skip first wake if it looks like flash/boot (often longer); keep next 10
    wakes = wakes_all
    if len(wakes) > MEASURED + 1:
        # drop first (post-flash) if duration outlier
        durs = [b - a for a, b in wakes]
        med = statistics.median(durs[1:MEASURED + 1] if len(durs) > MEASURED else durs)
        if durs[0] > med * 2.5:
            wakes = wakes[1:]
    wakes = wakes[:MEASURED]

    charges = []
    rows = []
    for i, (a, b) in enumerate(wakes):
        q = integrate_charge_mC(t, ua, a, b)
        charges.append(q)
        rows.append(
            {
                "i": i,
                "t_start_s": a,
                "t_end_s": b,
                "duration_ms": (b - a) * 1000.0,
                "charge_mC": q,
            }
        )
        print(
            f"Q{i+1}: start={a:.3f} end={b:.3f} dur={(b-a)*1000:.0f}ms q={q:.2f} mC",
            flush=True,
        )

    plots = save_plots(t, ua, wakes, OUT_DIR)

    # UDP: firmware starts at g_index=0; collect unique measured
    idxs = [e["idx"] for e in udp.events]
    unique = sorted(set(idxs))
    # count how many distinct indices arrived (any)
    rx_unique = len(unique)
    result = {
        "mode": "AETHER_DIAG_UDP_RTC_INDEX",
        "board_powerdown": False,
        "interval_s": 10,
        "udp_host": UDP_HOST,
        "udp_port": UDP_PORT,
        "branch": branch,
        "sha": sha,
        "voltage_mv": VOLTAGE_MV,
        "compile_fixes": [
            "PeripheralPowerDownForDeepSleep no-op under AETHER_DIAG_UDP_RTC_INDEX only"
        ],
        "sdkconfig_source": "experiments/_diag_udp_rtc_index_flash.py force_sdk (~35 mC reference comment)",
        "ppk_csv": str(raw_csv),
        "plots": plots,
        "udp_all_idx": idxs,
        "udp_unique_count": rx_unique,
        "wakes": rows,
        "summary": {
            "n": len(charges),
            "mean_mC": statistics.mean(charges) if charges else None,
            "median_mC": statistics.median(charges) if charges else None,
            "min_mC": min(charges) if charges else None,
            "max_mC": max(charges) if charges else None,
            "mean_duration_ms": statistics.mean(r["duration_ms"] for r in rows)
            if rows
            else None,
            "median_duration_ms": statistics.median(r["duration_ms"] for r in rows)
            if rows
            else None,
        },
        "acceptance_40_80_mC": (
            charges
            and 40.0 <= statistics.median(charges) <= 80.0
        ),
        "utc": datetime.now(timezone.utc).isoformat(),
    }
    JSON_OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2), flush=True)
    print(f"acceptance_40_80_mC={result['acceptance_40_80_mC']}", flush=True)
    print(f"wrote {JSON_OUT}", flush=True)

    # leave DUT powered; release serial handle if available
    try:
        if ppk.ppk and ppk.ppk.ser and ppk.ppk.ser.is_open:
            ppk.ppk.ser.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
