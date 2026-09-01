#!/usr/bin/env python3
"""Analyze PPK2 CSV traces from the prepared power-factor study.

Segments wake/active cycles using current edges around ~2000 ms deep-sleep
plateaus, discards incomplete first/last cycles, and expects exactly 100 cycles.
Computes mean/median/p90 cycle duration and energy at 3000 mV, plus bootstrap
95% CI for cycle energy vs variant A0 (id 0) baseline.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "experiments" / "power_modes_raw"
RESULTS = ROOT / "experiments" / "power_factor_results"

K_HOT_ATTEMPTS = 100
K_HOT_SLEEP_MS = 2000
VOLTAGE_MV = 3000
BOOTSTRAP_N = 5000
RNG_SEED = 42

# Plateau detection (uA): deep sleep should sit well below active wake current.
SLEEP_UA_MAX = 500.0
WAKE_UA_MIN = 2000.0
MIN_PLATEAU_S = 1.5
MAX_PLATEAU_S = 3.0
MIN_WAKE_S = 0.05
MAX_WAKE_S = 30.0


@dataclass
class Sample:
    t_s: float
    uA: float


@dataclass
class Cycle:
    index: int
    wake_start_s: float
    wake_end_s: float
    duration_s: float
    energy_uJ: float
    mean_uA: float
    sleep_plateau_s: float


def load_csv(path: Path) -> list[Sample]:
    rows: list[Sample] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            try:
                rows.append(Sample(float(row[0]), float(row[1])))
            except ValueError:
                continue
    if not rows:
        raise ValueError(f"no samples in {path}")
    return rows


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def integrate_energy(samples: list[Sample], t0: float, t1: float) -> tuple[float, float]:
    """Return (duration_s, energy_uJ) for [t0, t1] using trapezoidal rule."""
    if t1 <= t0:
        return 0.0, 0.0
    seg = [s for s in samples if t0 <= s.t_s <= t1]
    if len(seg) < 2:
        return t1 - t0, 0.0
    energy_nWs = 0.0
    for a, b in zip(seg, seg[1:]):
        dt = b.t_s - a.t_s
        if dt <= 0:
            continue
        avg_uA = 0.5 * (a.uA + b.uA)
        # nWs = uA * mV * s ; uJ = nWs / 1000
        energy_nWs += avg_uA * VOLTAGE_MV * dt
    duration = seg[-1].t_s - seg[0].t_s
    mean_uA = statistics.fmean(s.uA for s in seg)
    return duration, energy_nWs / 1000.0


def find_sleep_plateaus(samples: list[Sample]) -> list[tuple[float, float]]:
    """Return (start_s, end_s) intervals that look like deep-sleep plateaus."""
    if len(samples) < 3:
        return []
    plateaus: list[tuple[float, float]] = []
    i = 0
    n = len(samples)
    while i < n:
        if samples[i].uA > SLEEP_UA_MAX:
            i += 1
            continue
        start = samples[i].t_s
        j = i + 1
        while j < n and samples[j].uA <= SLEEP_UA_MAX:
            j += 1
        end = samples[j - 1].t_s
        dur = end - start
        if MIN_PLATEAU_S <= dur <= MAX_PLATEAU_S:
            plateaus.append((start, end))
        i = max(j, i + 1)
    return plateaus


def segment_cycles(samples: list[Sample]) -> list[Cycle]:
    """Segment active wake windows between sleep plateaus."""
    plateaus = find_sleep_plateaus(samples)
    if len(plateaus) < 2:
        raise ValueError("could not find enough sleep plateaus for segmentation")

    cycles: list[Cycle] = []
    for idx, ((sleep_a0, sleep_a1), (sleep_b0, _sleep_b1)) in enumerate(
        zip(plateaus, plateaus[1:])
    ):
        wake_start = sleep_a1
        wake_end = sleep_b0
        duration = wake_end - wake_start
        if duration < MIN_WAKE_S or duration > MAX_WAKE_S:
            continue
        seg = [s for s in samples if wake_start <= s.t_s <= wake_end]
        if not seg:
            continue
        peak = max(s.uA for s in seg)
        if peak < WAKE_UA_MIN:
            continue
        dur_i, energy = integrate_energy(samples, wake_start, wake_end)
        mean_uA = statistics.fmean(s.uA for s in seg)
        cycles.append(
            Cycle(
                index=idx,
                wake_start_s=wake_start,
                wake_end_s=wake_end,
                duration_s=dur_i,
                energy_uJ=energy,
                mean_uA=mean_uA,
                sleep_plateau_s=sleep_b0 - sleep_a0,
            )
        )

    if len(cycles) >= 3:
        cycles = cycles[1:-1]
    return cycles


def summarise_cycles(cycles: list[Cycle]) -> dict:
    durations = [c.duration_s for c in cycles]
    energies = [c.energy_uJ for c in cycles]
    means = [c.mean_uA for c in cycles]
    return {
        "n_cycles": len(cycles),
        "duration_s": {
            "mean": statistics.fmean(durations),
            "median": statistics.median(durations),
            "p90": pct(durations, 0.9),
        },
        "energy_uJ": {
            "mean": statistics.fmean(energies),
            "median": statistics.median(energies),
            "p90": pct(energies, 0.9),
        },
        "mean_uA": {
            "mean": statistics.fmean(means),
            "median": statistics.median(means),
            "p90": pct(means, 0.9),
        },
    }


def bootstrap_ratio_ci(
    variant: list[float], baseline: list[float], *, n: int = BOOTSTRAP_N
) -> dict:
    if not variant or not baseline:
        return {"ratio_median": None, "ci95_low": None, "ci95_high": None}
    rng = random.Random(RNG_SEED)
    ratios: list[float] = []
    for _ in range(n):
        v = statistics.fmean(rng.choice(variant) for _ in range(len(variant)))
        b = statistics.fmean(rng.choice(baseline) for _ in range(len(baseline)))
        if b > 0:
            ratios.append(v / b)
    if not ratios:
        return {"ratio_median": None, "ci95_low": None, "ci95_high": None}
    ratios.sort()
    return {
        "ratio_median": statistics.median(ratios),
        "ci95_low": pct(ratios, 0.025),
        "ci95_high": pct(ratios, 0.975),
    }


def load_baseline_energies(ap: str) -> list[float] | None:
    for base in (RAW_DIR / f"0_{ap}.csv", RAW_DIR / "0_chirkov.csv"):
        if not base.exists():
            continue
        try:
            cycles = segment_cycles(load_csv(base))
            return [c.energy_uJ for c in cycles]
        except ValueError:
            continue
    return None


def analyze_file(csv_path: Path, *, ap: str, variant_id: int) -> dict:
    samples = load_csv(csv_path)
    cycles = segment_cycles(samples)
    if len(cycles) != K_HOT_ATTEMPTS:
        raise ValueError(
            f"expected {K_HOT_ATTEMPTS} cycles after trimming, got {len(cycles)} "
            f"from {csv_path.name}"
        )
    summary = summarise_cycles(cycles)
    energies = [c.energy_uJ for c in cycles]
    baseline = load_baseline_energies(ap)
    vs_a0 = bootstrap_ratio_ci(energies, baseline or [])
    return {
        "csv": str(csv_path),
        "ap": ap,
        "variant_id": variant_id,
        "sleep_ms_target": K_HOT_SLEEP_MS,
        "voltage_mv": VOLTAGE_MV,
        "cycles_expected": K_HOT_ATTEMPTS,
        "summary": summary,
        "vs_a0_energy": vs_a0,
        "baseline_ap": ap if baseline else None,
    }


def write_tsv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "variant_id",
        "ap",
        "n_cycles",
        "duration_mean_s",
        "duration_median_s",
        "duration_p90_s",
        "energy_mean_uJ",
        "energy_median_uJ",
        "energy_p90_uJ",
        "vs_a0_ratio_median",
        "vs_a0_ci95_low",
        "vs_a0_ci95_high",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(header)
        for row in rows:
            s = row["summary"]
            v = row["vs_a0_energy"]
            w.writerow(
                [
                    row["variant_id"],
                    row["ap"],
                    s["n_cycles"],
                    f"{s['duration_s']['mean']:.6f}",
                    f"{s['duration_s']['median']:.6f}",
                    f"{s['duration_s']['p90']:.6f}",
                    f"{s['energy_uJ']['mean']:.3f}",
                    f"{s['energy_uJ']['median']:.3f}",
                    f"{s['energy_uJ']['p90']:.3f}",
                    "" if v["ratio_median"] is None else f"{v['ratio_median']:.4f}",
                    "" if v["ci95_low"] is None else f"{v['ci95_low']:.4f}",
                    "" if v["ci95_high"] is None else f"{v['ci95_high']:.4f}",
                ]
            )


def discover_csvs() -> list[tuple[Path, str, int]]:
    out: list[tuple[Path, str, int]] = []
    for path in sorted(RAW_DIR.glob("*_*.csv")):
        stem = path.stem
        if "_" not in stem:
            continue
        vid_s, ap = stem.split("_", 1)
        try:
            out.append((path, ap, int(vid_s)))
        except ValueError:
            continue
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="single raw CSV to analyze")
    parser.add_argument("--variant", type=int, help="variant id (with --csv)")
    parser.add_argument("--ap", type=str, help="access point (with --csv)")
    parser.add_argument(
        "--all",
        action="store_true",
        help="analyze every CSV under experiments/power_modes_raw/",
    )
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[Path, str, int]] = []
    if args.all:
        jobs = discover_csvs()
    elif args.csv:
        if args.variant is None or not args.ap:
            parser.error("--csv requires --variant and --ap")
        jobs = [(args.csv, args.ap, args.variant)]
    else:
        parser.error("pass --csv ... or --all")

    analyzed: list[dict] = []
    for csv_path, ap, variant_id in jobs:
        try:
            result = analyze_file(csv_path, ap=ap, variant_id=variant_id)
        except ValueError as exc:
            result = {
                "csv": str(csv_path),
                "ap": ap,
                "variant_id": variant_id,
                "error": str(exc),
            }
        out_json = RESULTS / f"{variant_id}_{ap}_ppk.json"
        out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        if "error" not in result:
            analyzed.append(result)
        print(f"{csv_path.name}: {result.get('error', 'ok')}")

    if analyzed:
        summary_json = RESULTS / "power_factor_ppk_summary.json"
        summary_json.write_text(json.dumps(analyzed, indent=2), encoding="utf-8")
        write_tsv(analyzed, RESULTS / "power_factor_ppk_summary.tsv")
        print(f"Wrote {summary_json} and power_factor_ppk_summary.tsv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
