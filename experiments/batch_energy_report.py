#!/usr/bin/env python3
"""Batch energy report for prepared power-factor PPK CSVs (spike-tolerant)."""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "experiments" / "power_modes_raw"
RESULTS = ROOT / "experiments" / "power_factor_results"
CHECKPOINT = ROOT / "experiments" / "power_factor_checkpoint.json"
OUT_TSV = RESULTS / "energy_report.tsv"
OUT_MD = ROOT / "experiments" / "PREPARED_POWER_FACTOR_ENERGY.md"
OUT_JSON = RESULTS / "energy_report.json"

VOLTAGE_MV = 3000.0
SLEEP_UA = 1000.0  # deep-sleep + noise margin
WAKE_PEAK_UA = 3000.0
MIN_SLEEP_S = 1.2
MAX_SLEEP_S = 3.5
MIN_WAKE_S = 0.02
MAX_WAKE_S = 25.0
STRIDE = 10
SPIKE_GAP_S = 0.08  # ignore brief spikes inside sleep


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


def load_decimated(path: Path, stride: int = STRIDE) -> tuple[list[float], list[float]]:
    ts: list[float] = []
    ua: list[float] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for i, row in enumerate(reader):
            if i % stride:
                continue
            if len(row) < 2:
                continue
            try:
                ts.append(float(row[0]))
                ua.append(float(row[1]))
            except ValueError:
                continue
    if len(ts) < 200:
        raise ValueError(f"too few samples in {path.name}: {len(ts)}")
    return ts, ua


def find_sleep_plateaus(ts: list[float], ua: list[float]) -> list[tuple[float, float, int, int]]:
    """Return (t0, t1, i0, i1) sleep plateaus; tolerate short spikes."""
    n = len(ts)
    plateaus: list[tuple[float, float, int, int]] = []
    i = 0
    while i < n:
        if ua[i] > SLEEP_UA:
            i += 1
            continue
        i0 = i
        j = i + 1
        last_low = i
        while j < n:
            if ua[j] <= SLEEP_UA:
                last_low = j
                j += 1
                continue
            # spike / wake candidate — allow short gap
            gap_start = j
            while j < n and ua[j] > SLEEP_UA:
                j += 1
            gap_end_t = ts[j - 1] if j > gap_start else ts[gap_start]
            gap_s = gap_end_t - ts[gap_start]
            if j < n and gap_s <= SPIKE_GAP_S:
                # resume sleep after spike
                continue
            # real wake — close plateau at last_low
            break
        i1 = last_low
        dur = ts[i1] - ts[i0]
        if MIN_SLEEP_S <= dur <= MAX_SLEEP_S:
            plateaus.append((ts[i0], ts[i1], i0, i1))
        i = max(j, i1 + 1)
    return plateaus


def integrate(ts: list[float], ua: list[float], i0: int, i1: int) -> tuple[float, float, float, float]:
    """Return duration_s, energy_uJ, mean_uA, peak_uA for index range inclusive."""
    if i1 <= i0:
        return 0.0, 0.0, 0.0, 0.0
    energy_nWs = 0.0
    peak = 0.0
    sum_u = 0.0
    count = 0
    for a, b in zip(range(i0, i1), range(i0 + 1, i1 + 1)):
        dt = ts[b] - ts[a]
        if dt <= 0:
            continue
        avg = 0.5 * (ua[a] + ua[b])
        energy_nWs += avg * VOLTAGE_MV * dt
        peak = max(peak, ua[a], ua[b])
        sum_u += ua[a]
        count += 1
    if count == 0:
        return 0.0, 0.0, 0.0, 0.0
    return ts[i1] - ts[i0], energy_nWs / 1000.0, sum_u / count, peak


def segment_cycles(ts: list[float], ua: list[float]) -> list[dict]:
    plateaus = find_sleep_plateaus(ts, ua)
    if len(plateaus) < 5:
        raise ValueError(f"sleep plateaus={len(plateaus)}")

    cycles: list[dict] = []
    for (_a0, a1, _ia0, ia1), (b0, _b1, ib0, _ib1) in zip(plateaus, plateaus[1:]):
        # wake = end of sleep A → start of sleep B
        # find indices
        i0 = ia1
        i1 = ib0
        dur = ts[i1] - ts[i0]
        if dur < MIN_WAKE_S or dur > MAX_WAKE_S:
            continue
        d, e, mean_u, peak = integrate(ts, ua, i0, i1)
        if peak < WAKE_PEAK_UA:
            continue
        cycles.append(
            {
                "duration_s": d,
                "energy_uJ": e,
                "mean_uA": mean_u,
                "peak_uA": peak,
                "sleep_s": b0 - _a0,
            }
        )

    if len(cycles) >= 3:
        cycles = cycles[1:-1]
    return cycles


def summarise(cycles: list[dict]) -> dict:
    e = [c["energy_uJ"] for c in cycles]
    d = [c["duration_s"] for c in cycles]
    m = [c["mean_uA"] for c in cycles]
    return {
        "n_cycles": len(cycles),
        "n_plateaus_hint": None,
        "energy_uJ_mean": statistics.fmean(e),
        "energy_uJ_median": statistics.median(e),
        "energy_uJ_p90": pct(e, 0.9),
        "energy_mJ_mean": statistics.fmean(e) / 1000.0,
        "energy_mJ_median": statistics.median(e) / 1000.0,
        "duration_s_mean": statistics.fmean(d),
        "duration_s_median": statistics.median(d),
        "mean_uA_mean": statistics.fmean(m),
        "total_energy_mJ": sum(e) / 1000.0,
        "energies_uJ": e,
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    cp = json.loads(CHECKPOINT.read_text(encoding="utf-8")) if CHECKPOINT.exists() else {}
    meta_map = cp.get("results", {})

    rows: list[dict] = []
    for csv_path in sorted(RAW.glob("*_*.csv")):
        stem = csv_path.stem
        meta = meta_map.get(stem, {})
        ap = meta.get("ap") or ("aethernetio" if "aethernetio" in stem else "chirkov")
        vid = meta.get("variant_id")
        if vid is None:
            try:
                vid = int(stem.split("_", 1)[0])
            except ValueError:
                vid = -1
        name = meta.get("variant_name", "")
        try:
            ts, ua = load_decimated(csv_path, STRIDE)
            plateaus = find_sleep_plateaus(ts, ua)
            cycles = segment_cycles(ts, ua)
            if len(cycles) < 70:
                raise ValueError(
                    f"cycles={len(cycles)} plateaus={len(plateaus)} (need >=70)"
                )
            summary = summarise(cycles)
            summary["n_plateaus"] = len(plateaus)
            row = {
                "key": stem,
                "ap": ap,
                "variant_id": vid,
                "variant_name": name,
                "hot_unique": (meta.get("rx") or {}).get("hot_unique"),
                "csv_mb": round(csv_path.stat().st_size / 1e6, 1),
                "status": "OK",
                "error": "",
                **{k: v for k, v in summary.items() if k != "energies_uJ"},
                "energies_uJ": summary["energies_uJ"],
            }
            rows.append(row)
            print(
                f"OK {stem}: n={summary['n_cycles']} plat={len(plateaus)} "
                f"E_med={summary['energy_mJ_median']:.3f} mJ "
                f"t_med={summary['duration_s_median']*1000:.1f} ms",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "key": stem,
                    "ap": ap,
                    "variant_id": vid,
                    "variant_name": name,
                    "hot_unique": (meta.get("rx") or {}).get("hot_unique"),
                    "csv_mb": round(csv_path.stat().st_size / 1e6, 1),
                    "status": "FAIL",
                    "error": str(exc),
                    "n_cycles": 0,
                    "energy_mJ_median": None,
                    "energy_mJ_mean": None,
                    "energy_uJ_median": None,
                    "energy_uJ_mean": None,
                    "energy_uJ_p90": None,
                    "duration_s_median": None,
                    "duration_s_mean": None,
                    "mean_uA_mean": None,
                    "total_energy_mJ": None,
                    "energies_uJ": [],
                }
            )
            print(f"FAIL {stem}: {exc}", flush=True)

    base = next((r for r in rows if r["key"] == "0_chirkov" and r["status"] == "OK"), None)
    base_e = base["energy_uJ_median"] if base else None
    # fallback baseline: first OK chirkov
    if base_e is None:
        base = next((r for r in rows if r["ap"] == "chirkov" and r["status"] == "OK"), None)
        base_e = base["energy_uJ_median"] if base else None

    rows.sort(key=lambda r: (0 if r["ap"] == "chirkov" else 1, r["variant_id"] or 0))

    # TSV
    headers = [
        "key",
        "ap",
        "variant_id",
        "variant_name",
        "status",
        "n_cycles",
        "hot_unique",
        "energy_mJ_median",
        "energy_mJ_mean",
        "energy_mJ_p90",
        "vs_baseline",
        "duration_ms_median",
        "mean_uA",
        "total_wake_energy_mJ",
        "csv_mb",
        "error",
    ]
    tsv = ["\t".join(headers)]
    for r in rows:
        vs = ""
        if base_e and r.get("energy_uJ_median"):
            vs = f"{r['energy_uJ_median'] / base_e:.3f}"
        p90 = (
            f"{r['energy_uJ_p90']/1000:.4f}"
            if r.get("energy_uJ_p90") is not None
            else ""
        )
        dur = (
            f"{r['duration_s_median']*1000:.2f}"
            if r.get("duration_s_median") is not None
            else ""
        )
        tsv.append(
            "\t".join(
                [
                    r["key"],
                    str(r["ap"]),
                    str(r["variant_id"]),
                    str(r["variant_name"]),
                    r["status"],
                    str(r.get("n_cycles") or 0),
                    str(r.get("hot_unique") or ""),
                    f"{r['energy_mJ_median']:.4f}" if r.get("energy_mJ_median") is not None else "",
                    f"{r['energy_mJ_mean']:.4f}" if r.get("energy_mJ_mean") is not None else "",
                    p90,
                    vs,
                    dur,
                    f"{r['mean_uA_mean']:.1f}" if r.get("mean_uA_mean") is not None else "",
                    f"{r['total_energy_mJ']:.2f}" if r.get("total_energy_mJ") is not None else "",
                    str(r["csv_mb"]),
                    (r.get("error") or "").replace("\t", " "),
                ]
            )
        )
    OUT_TSV.write_text("\n".join(tsv) + "\n", encoding="utf-8")

    # JSON without huge arrays trimmed
    slim = []
    for r in rows:
        slim.append({k: v for k, v in r.items() if k != "energies_uJ"})
    OUT_JSON.write_text(json.dumps(slim, indent=2), encoding="utf-8")

    ok = [r for r in rows if r["status"] == "OK"]
    ok_sorted = sorted(ok, key=lambda r: r["energy_mJ_median"] or 1e9)

    md: list[str] = [
        "# Prepared Power Factor — Energy Report",
        "",
        f"PPK2 @ **{VOLTAGE_MV:.0f} mV**. Spike-tolerant sleep plateaus "
        f"(I ≤ {SLEEP_UA:.0f} µA for 1.2–3.5 s, spikes ≤ {SPIKE_GAP_S*1000:.0f} ms ignored). "
        f"Cycle energy = ∫I·V over the **wake** window between consecutive sleep plateaus. "
        f"Decimation stride={STRIDE}. First/last cycles trimmed.",
        "",
        f"- CSVs OK: **{len(ok)}** / {len(rows)}",
        "- Missing capture: `1_chirkov`",
    ]
    if base:
        md.append(
            f"- Baseline for ratios: `{base['key']}` median "
            f"**{base['energy_mJ_median']:.3f} mJ**/cycle "
            f"(wake {base['duration_s_median']*1000:.1f} ms)"
        )
    md += [
        f"- TSV: `experiments/power_factor_results/energy_report.tsv`",
        f"- JSON: `experiments/power_factor_results/energy_report.json`",
        "",
        "## Ranking (lowest median wake energy)",
        "",
        "| # | key | variant | n | E_med mJ | E_mean mJ | vs base | wake_ms |",
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(ok_sorted, 1):
        vs = (
            f"{r['energy_uJ_median']/base_e:.3f}"
            if base_e and r.get("energy_uJ_median")
            else ""
        )
        md.append(
            f"| {i} | {r['key']} | {r['variant_name']} | {r['n_cycles']} | "
            f"{r['energy_mJ_median']:.3f} | {r['energy_mJ_mean']:.3f} | {vs} | "
            f"{r['duration_s_median']*1000:.1f} |"
        )

    md += [
        "",
        "## All variants",
        "",
        "| key | variant | n | E_med mJ | E_mean mJ | p90 mJ | vs base | wake_ms | RX |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        if r["status"] != "OK":
            md.append(
                f"| {r['key']} | {r['variant_name']} | FAIL | — | — | — | — | — | "
                f"{r.get('hot_unique') or ''} |"
            )
            continue
        vs = (
            f"{r['energy_uJ_median']/base_e:.3f}"
            if base_e and r.get("energy_uJ_median")
            else ""
        )
        md.append(
            f"| {r['key']} | {r['variant_name']} | {r['n_cycles']} | "
            f"{r['energy_mJ_median']:.3f} | {r['energy_mJ_mean']:.3f} | "
            f"{r['energy_uJ_p90']/1000:.3f} | {vs} | "
            f"{r['duration_s_median']*1000:.1f} | {r.get('hot_unique') or ''} |"
        )

    fails = [r for r in rows if r["status"] != "OK"]
    if fails:
        md += ["", "## Failures", ""]
        for r in fails:
            md.append(f"- `{r['key']}`: {r.get('error')}")

    md += [
        "",
        "## Interpretation",
        "",
        "- **E_med / E_mean**: median/mean energy of one HOT wake (prepare+TX+teardown), not including deep-sleep idle.",
        "- **vs base**: ratio of median cycle energy to baseline A0 (or first OK chirkov if A0 failed).",
        "- Absolute mJ depends on decimation; use **ratios and ranking** for comparing factors.",
        "- Variants with fewer cycles still usable if n≥70; check RX unique column for packet delivery.",
        "",
    ]
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nWrote {OUT_MD}")
    print(f"OK={len(ok)} FAIL={len(fails)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
