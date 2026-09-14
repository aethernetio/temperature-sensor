#!/usr/bin/env python3
"""Re-analyze historical UDP RTC PPK CSV with reconstructed timestamps."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "experiments" / "power_modes_raw" / "thermometer2_historical_udp_rtc"
OUT = ROOT / "experiments" / "power_factor_results" / "thermometer2_low_energy_plots"
JSON_OUT = (
    ROOT
    / "experiments"
    / "power_factor_results"
    / "thermometer2_historical_udp_rtc_repro.json"
)

# Capture used decim=20 from ~100 kHz PPK → 5 kHz effective.
DT_S = 20 * 10e-6
MEASURED = 10


def detect_wakes(
    t: np.ndarray,
    ua: np.ndarray,
    sleep_thr: float,
    wake_thr: float,
    min_wake_s: float = 0.08,
    min_sleep_s: float = 0.35,
) -> list[tuple[float, float]]:
    wakes: list[tuple[float, float]] = []
    i = 0
    n = len(t)
    dt = float(np.median(np.diff(t[:5000]))) if n > 10 else DT_S
    min_wake_n = max(3, int(min_wake_s / max(dt, 1e-9)))
    min_sleep_n = max(3, int(min_sleep_s / max(dt, 1e-9)))
    while i < n:
        while i < n and ua[i] < wake_thr:
            i += 1
        if i >= n:
            break
        j = i
        while j > 0 and ua[j - 1] > sleep_thr:
            j -= 1
        start = j
        k = i
        sleep_run = 0
        end = None
        while k < n:
            if ua[k] < sleep_thr:
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


def main() -> int:
    csv = sorted(RAW.glob("ppk_*.csv"))[-1]
    print(f"csv={csv}", flush=True)
    ua = np.loadtxt(csv, delimiter=",", skiprows=1, usecols=1)
    t = np.arange(len(ua), dtype=float) * DT_S
    print(
        f"n={len(ua)} dur={t[-1]:.3f}s "
        f"min/med/p90/max={ua.min():.3f}/{np.median(ua):.3f}/"
        f"{np.percentile(ua, 90):.1f}/{ua.max():.1f}",
        flush=True,
    )

    # Deep-sleep floor exists (~5 µA median). Use absolute deep-sleep baseline.
    sleep_thr = 200.0
    wake_thr = 2000.0
    wakes = detect_wakes(t, ua, sleep_thr, wake_thr)
    print(f"detected_wakes={len(wakes)}", flush=True)
    if len(wakes) > MEASURED + 1:
        durs = [b - a for a, b in wakes]
        med = statistics.median(durs[1 : MEASURED + 1])
        if durs[0] > med * 2.5:
            wakes = wakes[1:]
            print("dropped first long wake", flush=True)
    wakes = wakes[:MEASURED]

    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, (a, b) in enumerate(wakes):
        m = (t >= a) & (t <= b)
        q = float(np.trapezoid(ua[m] * 1e-6, t[m])) * 1000.0
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
            f"Q{i+1}: {a:.3f}->{b:.3f} {(b-a)*1000:.0f}ms {q:.2f}mC",
            flush=True,
        )

    qs = [r["charge_mC"] for r in rows]
    ds = [r["duration_ms"] for r in rows]
    summary = {
        "n": len(qs),
        "mean_mC": statistics.mean(qs) if qs else None,
        "median_mC": statistics.median(qs) if qs else None,
        "min_mC": min(qs) if qs else None,
        "max_mC": max(qs) if qs else None,
        "mean_duration_ms": statistics.mean(ds) if ds else None,
        "median_duration_ms": statistics.median(ds) if ds else None,
        "sleep_thr_uA": sleep_thr,
        "wake_thr_uA": wake_thr,
        "dt_s": DT_S,
    }
    print(json.dumps(summary, indent=2), flush=True)

    fig, ax = plt.subplots(figsize=(14, 5))
    step = max(1, len(t) // 200000)
    ax.plot(t[::step], ua[::step], lw=0.4)
    for i, (a, b) in enumerate(wakes):
        ax.axvspan(a, b, color="orange", alpha=0.25)
        ax.text((a + b) / 2, 1e5, str(i), ha="center", fontsize=8)
    ax.axhline(sleep_thr, color="gray", ls=":", label=f"sleep_thr={sleep_thr:.0f}")
    ax.set_yscale("symlog", linthresh=100)
    ax.set_title("Historical UDP RTC — 10 wakes (visual boundaries)")
    ax.set_xlabel("t (s)")
    ax.set_ylabel("I (uA)")
    ax.legend()
    fig.tight_layout()
    overview = OUT / "historical_10_wakes_overview.png"
    fig.savefig(overview, dpi=120)
    plt.close(fig)

    zooms = []
    for i, (a, b) in enumerate(wakes):
        pad = max(0.25, (b - a) * 0.4)
        m = (t >= a - pad) & (t <= b + pad)
        fig, ax = plt.subplots(figsize=(10, 4))
        ts, us = t[m], ua[m]
        step = max(1, len(ts) // 80000)
        ax.plot(ts[::step], us[::step], lw=0.6)
        ax.axvline(a, color="g", ls="--", label="wake start")
        ax.axvline(b, color="r", ls="--", label="sleep entry")
        ax.axvspan(a, b, color="orange", alpha=0.2)
        ax.set_yscale("symlog", linthresh=100)
        ax.set_title(
            f"Wake {i}: {(b-a)*1000:.0f} ms, {rows[i]['charge_mC']:.1f} mC"
        )
        ax.legend(fontsize=8)
        fig.tight_layout()
        z = OUT / f"historical_wake_{i:02d}_zoom.png"
        fig.savefig(z, dpi=120)
        plt.close(fig)
        zooms.append(str(z))

    # Sleep current between wakes (stable baseline samples)
    sleep_mask = ua < sleep_thr
    sleep_avg = float(np.mean(ua[sleep_mask])) if np.any(sleep_mask) else None
    sleep_med = float(np.median(ua[sleep_mask])) if np.any(sleep_mask) else None

    result = {
        "mode": "AETHER_DIAG_UDP_RTC_INDEX",
        "board_powerdown": False,
        "interval_s": 10,
        "udp_host": "192.168.68.84",
        "udp_port": 9000,
        "voltage_mv": 3000,
        "compile_fixes": [
            "PeripheralPowerDownForDeepSleep no-op under AETHER_DIAG_UDP_RTC_INDEX only"
        ],
        "sdkconfig_source": "experiments/_diag_udp_rtc_index_flash.py force_sdk",
        "ppk_csv": str(csv),
        "timestamp_reconstruction": "uniform DT_S=200us (decim 20 @ 100kHz); host clock stamps were chunk-constant",
        "plots": {"overview": str(overview), "zooms": zooms},
        "wakes": rows,
        "summary": summary,
        "sleep_between_wakes_uA": {"avg": sleep_avg, "median": sleep_med},
        "udp_note": "capture log showed idx 0..14 delivered (~10s cadence)",
        "acceptance_40_80_mC": bool(qs and 40.0 <= statistics.median(qs) <= 80.0),
        "boundaries_method": "rise>2mA back to first >200uA; end when |I|<200uA sustained 350ms (actual deep-sleep floor ~5uA present even without BoardPowerDown in this capture)",
    }
    JSON_OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"acceptance_40_80_mC={result['acceptance_40_80_mC']} wrote={JSON_OUT}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
