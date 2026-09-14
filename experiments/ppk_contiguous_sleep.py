#!/usr/bin/env python3
"""Raw contiguous deep-sleep current analysis (no sample-current filtering).

Pass/fail sleep current MUST use every sample in a time window.
Thresholds are allowed only to locate wake edges.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


def load_ua_csv(path, dt_s: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Return (t_s, ua). If CSV has t_s column use it; else reconstruct with dt_s."""
    data = np.loadtxt(path, delimiter=",", skiprows=1)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.shape[1] >= 2:
        t = data[:, 0].astype(float)
        ua = data[:, 1].astype(float)
        return t, ua
    if dt_s is None:
        raise ValueError("need dt_s when CSV has only current column")
    ua = data[:, 0].astype(float)
    t = np.arange(len(ua), dtype=float) * dt_s
    return t, ua


def contiguous_stats(t: np.ndarray, ua: np.ndarray, t0: float, t1: float) -> dict[str, Any]:
    """All samples in [t0, t1] — no magnitude filter. Cross-check mean vs Q/T."""
    if t1 <= t0:
        raise ValueError(f"empty window t0={t0} t1={t1}")
    m = (t >= t0) & (t <= t1)
    if not np.any(m):
        raise ValueError(f"no samples in [{t0},{t1}]")
    tt = t[m]
    uu = ua[m]
    duration_s = float(tt[-1] - tt[0]) if len(tt) > 1 else float(t1 - t0)
    if duration_s <= 0:
        duration_s = float(t1 - t0)
    # Charge via trapezoid of Amperes vs seconds → Coulombs
    charge_C = float(np.trapezoid(uu * 1e-6, tt))
    i_from_charge_A = charge_C / duration_s if duration_s > 0 else float("nan")
    i_mean_A = float(np.mean(uu)) * 1e-6
    i_from_charge_uA = i_from_charge_A * 1e6
    i_mean_uA = i_mean_A * 1e6
    # Sanity: mean vs Q/T must agree within 5% (or absolute 1 µA when near zero)
    denom = max(abs(i_mean_uA), 1.0)
    rel_err = abs(i_mean_uA - i_from_charge_uA) / denom
    ok = rel_err < 0.05 or abs(i_mean_uA - i_from_charge_uA) < 1.0
    # Hard fail: hundreds of mC over tens of seconds cannot be "µA"
    if duration_s >= 5.0 and charge_C >= 0.05 and i_mean_uA < 100.0:
        ok = False
        rel_err = float("inf")
    return {
        "t0_s": float(t0),
        "t1_s": float(t1),
        "duration_s": duration_s,
        "n_samples": int(np.count_nonzero(m)),
        "charge_C": charge_C,
        "charge_mC": charge_C * 1000.0,
        "avg_uA": i_mean_uA,
        "avg_from_charge_uA": i_from_charge_uA,
        "median_uA": float(np.median(uu)),
        "p95_uA": float(np.percentile(uu, 95)),
        "max_uA": float(np.max(uu)),
        "min_uA": float(np.min(uu)),
        "q_over_t_rel_err": float(rel_err),
        "q_over_t_ok": bool(ok),
        "sample_filter_used": False,
    }


def assert_q_over_t(stats: dict[str, Any]) -> None:
    if not stats.get("q_over_t_ok"):
        raise RuntimeError(
            "HARD FAIL analyzer/physics cross-check: "
            f"mean={stats.get('avg_uA')} uA vs Q/T={stats.get('avg_from_charge_uA')} uA "
            f"Q={stats.get('charge_mC')} mC T={stats.get('duration_s')} s "
            f"rel_err={stats.get('q_over_t_rel_err')}"
        )


def find_wake_segments(
    t: np.ndarray,
    ua: np.ndarray,
    active_uA: float = 1500.0,
    min_dur_s: float = 0.15,
    step: int = 50,
) -> list[tuple[float, float]]:
    """Locate active wakes via I > active_uA on a downsampled envelope."""
    u = ua[::step]
    tt = t[::step]
    active = u > active_uA
    changes = np.diff(active.astype(int))
    starts = np.where(changes == 1)[0] + 1
    ends = np.where(changes == -1)[0] + 1
    if active[0]:
        starts = np.r_[0, starts]
    if active[-1]:
        ends = np.r_[ends, len(active) - 1]
    segs: list[tuple[float, float]] = []
    for s, e in zip(starts, ends):
        dur = (e - s) * (tt[1] - tt[0] if len(tt) > 1 else 0.01)
        if dur >= min_dur_s:
            a = float(tt[s])
            b = float(tt[min(e, len(tt) - 1)])
            segs.append((a, b))
    return segs


def sleep_windows_between_wakes(
    wakes: list[tuple[float, float]],
    t_end: float,
    guard_s: float = 2.0,
    min_window_s: float = 20.0,
) -> list[tuple[float, float]]:
    """Contiguous sleep intervals between wake ends/starts with guards."""
    wins: list[tuple[float, float]] = []
    for i, (_a, b) in enumerate(wakes):
        if i + 1 < len(wakes):
            nxt = wakes[i + 1][0]
        else:
            nxt = t_end
        t0 = b + guard_s
        t1 = nxt - guard_s
        if t1 - t0 >= min_window_s:
            wins.append((t0, t1))
    return wins


def sleep_pass(stats: dict[str, Any], target_uA: float = 16.0, ok_uA: float = 25.0) -> str:
    assert_q_over_t(stats)
    avg = float(stats["avg_uA"])
    if avg <= target_uA:
        return "TARGET"
    if avg <= ok_uA:
        return "INTERMEDIATE"
    return "FAIL"
