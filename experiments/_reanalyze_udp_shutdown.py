#!/usr/bin/env python3
"""Reanalyze decimated PPK CSV with robust sleep mask."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "experiments/power_factor_results/udp_1min_peripheral_shutdown.json"


def main() -> None:
    js = json.loads(JS_PATH.read_text(encoding="utf-8"))
    csv = Path(js["csv"])
    data = np.loadtxt(csv, delimiter=",", skiprows=1)
    ts, us = data[:, 0], data[:, 1]
    print(
        "csv samples",
        len(us),
        "dur",
        ts[-1] - ts[0],
        "mean",
        float(np.mean(us)),
    )

    win = 200  # 200 ms at 1 kHz
    thr = 500.0
    below = (np.abs(us) < thr).astype(np.float64)
    ker = np.ones(win) / win
    frac = np.convolve(below, ker, mode="same")
    is_sleep = frac > 0.90
    is_wake = frac < 0.50

    wake_edges: list[float] = []
    sleep_edges: list[float] = []
    state = "sleep" if bool(is_sleep[0]) else "wake"
    for i in range(1, len(us)):
        if state == "sleep" and bool(is_wake[i]):
            wake_edges.append(float(ts[i]))
            state = "wake"
        elif state == "wake" and bool(is_sleep[i]):
            sleep_edges.append(float(ts[i]))
            state = "sleep"
    print("wakes", len(wake_edges), wake_edges[:12])
    print("sleeps", len(sleep_edges), sleep_edges[:12])

    measured = 10
    if len(wake_edges) >= measured + 1:
        w = wake_edges[1 : 1 + measured]
    else:
        w = wake_edges[:measured]

    s: list[float | None] = []
    for wi in w:
        cands = [x for x in sleep_edges if x > wi]
        s.append(cands[0] if cands else None)

    act: list[dict] = []
    slp: list[dict] = []
    for i, wi in enumerate(w):
        si = s[i]
        if si is None:
            continue
        m = (ts >= wi) & (ts < si)
        if not np.any(m):
            continue
        wt, wu = ts[m], us[m]
        dur = float(wt[-1] - wt[0])
        q = float(np.trapezoid(wu, wt) * 1e-6)
        act.append(
            {
                "dur": dur,
                "mean": float(np.mean(wu)),
                "q_mC": q * 1000,
                "med": float(np.median(wu)),
            }
        )
        next_w = w[i + 1] if i + 1 < len(w) else float(ts[-1])
        guard = 0.5
        m2 = (ts >= si + guard) & (ts < next_w - guard)
        if np.any(m2):
            st, su = ts[m2], us[m2]
            dur2 = float(st[-1] - st[0])
            q2 = float(np.trapezoid(su, st) * 1e-6)
            slp.append(
                {
                    "dur": dur2,
                    "mean": float(np.mean(su)),
                    "med": float(np.median(su)),
                    "q_mC": q2 * 1000,
                }
            )

    print("active n", len(act))
    if act:
        print(
            "active mC",
            statistics.mean(x["q_mC"] for x in act),
            statistics.median(x["q_mC"] for x in act),
            min(x["q_mC"] for x in act),
            max(x["q_mC"] for x in act),
        )
    print("sleep n", len(slp))
    if slp:
        print(
            "sleep mean_uA",
            statistics.mean(x["mean"] for x in slp),
            statistics.median(x["mean"] for x in slp),
            max(x["mean"] for x in slp),
        )
        print("sleep med_uA avg", statistics.mean(x["med"] for x in slp))

    n = min(len(act), len(slp), measured)
    if n < 1:
        raise SystemExit("no cycles")

    t0 = w[0]
    last_sleep = s[n - 1]
    assert last_sleep is not None
    next_after = [x for x in wake_edges if x > last_sleep]
    t1 = next_after[0] if next_after else float(ts[-1])
    m = (ts >= t0) & (ts <= t1)
    wt, wu = ts[m], us[m]
    dur = float(wt[-1] - wt[0])
    q_dec = float(np.trapezoid(wu, wt) * 1e-6)

    # Prefer scaling full-rate total charge by time fraction of measured window.
    fr_q = float(js["capture"]["total_charge_C"])
    fr_dur = float(js["capture"]["duration_s"])
    q = fr_q * (dur / fr_dur) if fr_dur > 0 else q_dec
    q_cycle = q / n
    i_uA = (q_cycle / 60.0) * 1e6
    life_h = 800.0 / (i_uA / 1000.0)

    js["active"] = {
        "mean_mC": statistics.mean(x["q_mC"] for x in act[:n]),
        "median_mC": statistics.median(x["q_mC"] for x in act[:n]),
        "min_mC": min(x["q_mC"] for x in act[:n]),
        "max_mC": max(x["q_mC"] for x in act[:n]),
        "n": n,
    }
    js["sleep_raw"] = {
        "mean_uA": statistics.mean(x["mean"] for x in slp[:n]),
        "median_uA": statistics.mean(x["med"] for x in slp[:n]),
        "worst_uA": max(x["mean"] for x in slp[:n]),
        "mean_of_window_means_uA": statistics.mean(x["mean"] for x in slp[:n]),
        "median_of_window_means_uA": statistics.median(x["mean"] for x in slp[:n]),
        "n": n,
        "filtering_used": False,
        "despike_used_for_result": False,
        "method": "rolling_frac_below_500uA_on_1kHz_decimated; median_uA=avg of per-window sample medians",
    }
    js["full_cycle"] = {
        "n_cycles": n,
        "duration_s": dur,
        "total_charge_C": q,
        "charge_per_1min_cycle_mC": q_cycle * 1000.0,
        "average_current_uA": i_uA,
        "life_hours": life_h,
        "life_days": life_h / 24.0,
        "life_months": life_h / 24.0 / 30.44,
        "charge_source": "fullrate_total_scaled_by_time_fraction",
        "decimated_window_charge_C": q_dec,
    }
    js["analysis_method"] = "reanalyze_decimated_rolling_sleep_mask"
    JS_PATH.write_text(json.dumps(js, indent=2), encoding="utf-8")

    md = ROOT / "experiments/UDP_1MIN_PERIPHERAL_SHUTDOWN.md"
    md.write_text(
        "\n".join(
            [
                "# UDP 1-min + verified peripheral shutdown",
                "",
                f"- UDP: attempts={js['udp']['attempts']} RX={js['udp']['rx_unique']} "
                f"missing={js['udp']['missing']}",
                f"- GPIO: 17=LOW 2=HIGH 18/6/7=DISABLED",
                f"- active mean_mC={js['active']['mean_mC']:.2f} "
                f"median={js['active']['median_mC']:.2f} "
                f"min={js['active']['min_mC']:.2f} max={js['active']['max_mC']:.2f}",
                f"- sleep raw mean_uA={js['sleep_raw']['mean_uA']:.1f} "
                f"median={js['sleep_raw']['median_uA']:.1f} "
                f"worst={js['sleep_raw']['worst_uA']:.1f} "
                f"(window-mean median={js['sleep_raw']['median_of_window_means_uA']:.1f})",
                f"- Q/cycle_mC={js['full_cycle']['charge_per_1min_cycle_mC']:.2f} "
                f"Iavg_uA={js['full_cycle']['average_current_uA']:.1f}",
                f"- CR2 life_days={js['full_cycle']['life_days']:.2f}",
                f"- validation median_uA={js['validation']['median_uA']:.2f}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print("updated", JS_PATH)
    print(json.dumps(js["full_cycle"], indent=2))
    print(json.dumps(js["active"], indent=2))
    print(json.dumps(js["sleep_raw"], indent=2))


if __name__ == "__main__":
    main()
