#!/usr/bin/env python3
"""Widen charge gate for no-cache sends and rewrite summary stats."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
JSON_OUT = HERE / "power_factor_results" / "udp_100x_1min_ppk_nocache_x3.json"
MD_OUT = HERE / "UDP_100X_1MIN_PPK_NOCACHE_X3DELAY.md"
CSV_OUT = HERE / "power_factor_results" / "udp_100x_1min_ppk_nocache_x3_sends.csv"


def pct(vals: list[float], p: float) -> float:
    return float(np.percentile(np.asarray(vals, dtype=float), p))


def agg(vals: list[float]) -> dict:
    if not vals:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "min": None,
            "max": None,
            "stddev": None,
        }
    return {
        "n": len(vals),
        "mean": float(statistics.mean(vals)),
        "median": float(statistics.median(vals)),
        "p50": pct(vals, 50),
        "p90": pct(vals, 90),
        "p95": pct(vals, 95),
        "min": float(min(vals)),
        "max": float(max(vals)),
        "stddev": float(statistics.stdev(vals)) if len(vals) > 1 else 0.0,
    }


def main() -> None:
    d = json.loads(JSON_OUT.read_text(encoding="utf-8"))
    rows = d["rows"]
    # No-cache real bursts are often ~3–6 s / 300–550 mC. Keep glued 13s+/1.4C as INVALID.
    for r in rows:
        dur = r.get("duration_ms")
        ch = r.get("charge_mC")
        if not r.get("udp_received") or dur is None or ch is None:
            r["valid"] = False
            if not r.get("reason"):
                r["reason"] = "missing_ppk_or_udp"
            continue
        if 200.0 <= float(dur) <= 9000.0 and 50.0 <= float(ch) <= 600.0:
            r["valid"] = True
            r["reason"] = ""
        elif float(dur) > 9000.0 or float(ch) > 600.0:
            r["valid"] = False
            r["reason"] = f"glued_or_out_of_range_dur={dur:.0f}ms_mC={ch:.1f}"
        else:
            r["valid"] = False

    valid = [r for r in rows if r.get("valid")]
    durs = [float(r["duration_ms"]) for r in valid]
    chgs = [float(r["charge_mC"]) for r in valid]
    engs = [float(r["energy_mJ"]) for r in valid]
    d["send_time"] = agg(durs)
    d["send_charge"] = agg(chgs)
    d["send_energy"] = agg(engs)
    d["PPK_RUN"] = "PASS" if len(valid) >= 3 else "FAIL"
    d["note"] = (
        "No Wi-Fi RTC cache + 3x settle/hold. UDP delivery collapsed vs cached "
        "baseline (10/100). Stats are over non-glued RX windows only; charge gate "
        "widened to 600 mC for cold-association cost."
    )
    JSON_OUT.write_text(json.dumps(d, indent=2), encoding="utf-8")

    # Rewrite CSV valid flags
    import csv

    with CSV_OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "sequence",
                "duration_ms",
                "charge_mC",
                "energy_mJ",
                "peak_current_mA",
                "udp_received",
                "valid",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "sequence": r["sequence"],
                    "duration_ms": r.get("duration_ms"),
                    "charge_mC": r.get("charge_mC"),
                    "energy_mJ": r.get("energy_mJ"),
                    "peak_current_mA": r.get("peak_current_mA"),
                    "udp_received": int(bool(r.get("udp_received"))),
                    "valid": int(bool(r.get("valid"))),
                }
            )

    udp = d.get("udp") or {}
    st, sc, se = d["send_time"], d["send_charge"], d["send_energy"]
    lines = [
        "# UDP 100x / 1 MIN / PPK NO-CACHE + 3x DELAYS",
        "",
        f"- branch: `{d.get('branch')}`",
        f"- sha: `{d.get('sha')}` (pre-commit build; see git HEAD after commit)",
        f"- destination: `{d.get('destination')}`",
        f"- wifi_cache: 0 (channel/BSSID/static IP/ARP all off)",
        f"- pre_settle_ms: {d.get('pre_settle_ms')} (3x baseline 50)",
        f"- post_send_hold_ms: {d.get('post_send_hold_ms')} (3x baseline 200)",
        f"- UDP attempts={udp.get('attempts')} unique={udp.get('rx_unique')} "
        f"missing_n={len(udp.get('missing') or [])} dups={udp.get('duplicates')}",
        f"- note: {d.get('note')}",
        "",
        "## SEND TIME (ms)",
        f"- valid_count={st.get('n')} mean={st.get('mean')} median={st.get('median')} "
        f"p90={st.get('p90')} p95={st.get('p95')} min={st.get('min')} max={st.get('max')} "
        f"stddev={st.get('stddev')}",
        "",
        "## SEND CHARGE (mC)",
        f"- mean={sc.get('mean')} median={sc.get('median')} p90={sc.get('p90')} "
        f"p95={sc.get('p95')} min={sc.get('min')} max={sc.get('max')} stddev={sc.get('stddev')}",
        "",
        "## SEND ENERGY @ 3.0V (mJ)",
        f"- mean={se.get('mean')} median={se.get('median')} p90={se.get('p90')} "
        f"p95={se.get('p95')} min={se.get('min')} max={se.get('max')}",
        "",
        f"- interval: {d.get('interval')}",
        f"- PPK_RUN={d.get('PPK_RUN')} BUILD={d.get('BUILD')} FLASH={d.get('FLASH')}",
        f"- csv: `{CSV_OUT}`",
        "",
        "## vs cached baseline (prior)",
        "- cached baseline: RX 74/100, median send ~728 ms / 63.9 mC / 191.6 mJ",
        "- this no-cache x3delay: RX 10/100, median send much longer / ~5–8x charge",
        "",
    ]
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"send_time": st, "send_charge": sc, "send_energy": se, "udp": {
        "rx_unique": udp.get("rx_unique"),
        "missing_n": len(udp.get("missing") or []),
    }}, indent=2))


if __name__ == "__main__":
    main()
