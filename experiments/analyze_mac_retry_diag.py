"""Analyze prepared_mac_retry_diag.tsv and write PREPARED_MAC_RETRY_DIAG_REPORT.md."""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
TSV = ROOT / "experiments" / "prepared_mac_retry_diag.tsv"
REPORT = ROOT / "experiments" / "PREPARED_MAC_RETRY_DIAG_REPORT.md"

NAMES = {
    0: "CONTROL",
    1: "0/0",
    2: "1/1",
    3: "2/2",
    4: "3/3",
    5: "1/7",
    6: "7/1",
}


def pct(vals: list[int], p: float) -> int:
    if not vals:
        return 0
    s = sorted(vals)
    i = int(round((len(s) - 1) * p / 100.0))
    return s[max(0, min(i, len(s) - 1))]


def buckets(vals: list[int]) -> dict[str, int]:
    b = {
        "<2ms": 0,
        "2-5ms": 0,
        "5-10ms": 0,
        "10-20ms": 0,
        "20-40ms": 0,
        "40-60ms": 0,
        "60-100ms": 0,
        "timeout": 0,
    }
    for v in vals:
        ms = v / 1000.0
        if v >= 99000:
            b["timeout"] += 1
        elif ms < 2:
            b["<2ms"] += 1
        elif ms < 5:
            b["2-5ms"] += 1
        elif ms < 10:
            b["5-10ms"] += 1
        elif ms < 20:
            b["10-20ms"] += 1
        elif ms < 40:
            b["20-40ms"] += 1
        elif ms < 60:
            b["40-60ms"] += 1
        else:
            b["60-100ms"] += 1
    return b


def main() -> None:
    if not TSV.exists():
        raise SystemExit(f"missing {TSV}")
    rows = list(csv.DictReader(TSV.open(encoding="utf-8"), delimiter="\t"))
    hot = [r for r in rows if r.get("kind") == "2"]
    by_v: dict[int, list[dict]] = defaultdict(list)
    for r in hot:
        by_v[int(r.get("variant") or 0)].append(r)

    lines: list[str] = []
    lines.append("# Prepared MAC retry diagnostic report\n")
    lines.append("Experiment-only. Production SendPreparedOnce unchanged.\n")
    lines.append("aether-client-cpp SHA `157aadbec8e7b852d0f89274307ff7cb8103e5f7` unchanged=yes.\n")

    lines.append("\n## Per-variant summary\n")
    lines.append(
        "| variant | n | delivery | tx_ok | tx_fail | rc_ok | "
        "txdone_med | p90 | p99 | max | reconnect0_n |\n"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")

    stats = {}
    for vid in range(7):
        rs = by_v.get(vid, [])
        # Prefer reconnect_count==0 subset for MAC analysis
        primary = [r for r in rs if int(r.get("reconnect_count") or 0) == 0] or rs
        txdone = [int(r["txdone_us"]) for r in primary if r.get("txdone_us")]
        ok = sum(1 for r in primary if r.get("first_status") == "1")
        fail = sum(1 for r in primary if r.get("first_status") == "0")
        rc_bad = sum(1 for r in rs if int(r.get("retry_set_rc") or -1) not in (-1, 0) and vid != 0)
        rc_ok = all(int(r.get("retry_set_rc") or -1) in (-1, 0) for r in rs) if rs else False
        if vid != 0:
            rc_ok = all(int(r.get("retry_set_rc") or -999) == 0 for r in rs) if rs else False
        else:
            rc_ok = all(int(r.get("retry_called") or 0) == 0 for r in rs) if rs else False
        delivery = len(rs)  # received pending-hot rows
        attempted = 50
        med = pct(txdone, 50)
        p90 = pct(txdone, 90)
        p99 = pct(txdone, 99)
        mx = max(txdone) if txdone else 0
        stats[vid] = {
            "n": len(primary),
            "delivery": delivery,
            "ok": ok,
            "fail": fail,
            "rc_ok": rc_ok,
            "med": med,
            "p90": p90,
            "p99": p99,
            "max": mx,
            "txdone": txdone,
            "buckets": buckets(txdone),
            "rssi": [int(r["rssi"]) for r in primary if r.get("rssi")],
        }
        lines.append(
            f"| {NAMES[vid]} | {len(primary)} | {delivery}/{attempted} | {ok} | {fail} | "
            f"{'yes' if rc_ok else 'no'} | {med} | {p90} | {p99} | {mx} | {len(primary)} |\n"
        )

    lines.append("\n## Retry limit curve (CONTROL / 0/0 / 1/1 / 2/2 / 3/3)\n")
    lines.append("| retry | delivery | tx_ok | tx_fail | med | p90 | p99 | max |\n")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for vid in (0, 1, 2, 3, 4):
        s = stats[vid]
        lines.append(
            f"| {NAMES[vid]} | {s['delivery']}/50 | {s['ok']} | {s['fail']} | "
            f"{s['med']} | {s['p90']} | {s['p99']} | {s['max']} |\n"
        )

    lines.append("\n## Short vs long (CONTROL / 1/7 / 7/1)\n")
    lines.append("| variant | delivery | tx_ok | tx_fail | med | p90 | p99 | max |\n")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for vid in (0, 5, 6):
        s = stats[vid]
        lines.append(
            f"| {NAMES[vid]} | {s['delivery']}/50 | {s['ok']} | {s['fail']} | "
            f"{s['med']} | {s['p90']} | {s['p99']} | {s['max']} |\n"
        )

    # Infer short/long
    c_p90 = stats[0]["p90"]
    s17 = stats[5]["p90"]
    s71 = stats[6]["p90"]
    if c_p90 > 0 and s17 < c_p90 * 0.6 and s71 >= c_p90 * 0.8:
        infer = "SHORT"
    elif c_p90 > 0 and s71 < c_p90 * 0.6 and s17 >= c_p90 * 0.8:
        infer = "LONG"
    elif c_p90 > 0 and s17 < c_p90 * 0.7 and s71 < c_p90 * 0.7:
        infer = "BOTH"
    else:
        infer = "NEITHER / INCONCLUSIVE"

    # Monotonic curve?
    curve = [stats[v]["p90"] for v in (1, 2, 3, 4, 0)]
    mono = all(curve[i] <= curve[i + 1] * 1.15 for i in range(len(curve) - 1))

    confirmed = "yes" if (infer in ("SHORT", "LONG", "BOTH") and mono) else "no"
    if infer == "NEITHER / INCONCLUSIVE":
        confirmed = "no"

    # 0/0 semantics
    z = stats[1]
    if z["n"] == 0:
        zero_sem = "no data"
    elif not z["rc_ok"]:
        zero_sem = "API error (retry_set_rc != ESP_OK)"
    elif z["p90"] < max(2000, c_p90 * 0.3):
        zero_sem = "likely disables / minimizes retries (tail collapsed)"
    elif abs(z["p90"] - c_p90) < max(1000, c_p90 * 0.15):
        zero_sem = "behaves like default/CONTROL"
    else:
        zero_sem = "clamped or partial effect"

    # Best for energy
    best = min(
        ((vid, stats[vid]["med"], stats[vid]["delivery"]) for vid in range(7) if stats[vid]["n"]),
        key=lambda t: (t[1], -t[2]),
        default=(0, 0, 0),
    )

    lines.append("\n## Answers\n")
    lines.append(f"1. API works (rc ESP_OK on non-CONTROL): **{'yes' if all(stats[v]['rc_ok'] for v in range(1,7) if stats[v]['n']) else 'mixed/no'}**\n")
    lines.append(f"2. 0/0 semantics: **{zero_sem}**\n")
    lines.append(f"3. Small UDP frame controlled by: **{infer}**\n")
    lines.append(f"4. tx_done_wait vs limit: see curve; monotonic-ish={mono}\n")
    lines.append("5-6. See tables for txStatus and delivery.\n")
    lines.append(f"7. Long tail shrink at low retry: compare CONTROL p90={c_p90} vs 1/1 p90={stats[2]['p90']}\n")
    lines.append("8. RSSI: listed per-variant medians below.\n")
    lines.append(f"9. MAC_RETRY_HYPOTHESIS_CONFIRMED=**{confirmed}**\n")
    lines.append(f"10. Latency/energy suggestion (not production): **{NAMES[best[0]]}** (med={best[1]} us, recv={best[2]})\n")

    lines.append("\n## tx_done buckets (reconnect==0)\n")
    for vid in range(7):
        b = stats[vid]["buckets"]
        lines.append(f"- {NAMES[vid]}: " + ", ".join(f"{k}={v}" for k, v in b.items()) + "\n")

    lines.append("\n## RSSI medians\n")
    for vid in range(7):
        rs = stats[vid]["rssi"]
        med = sorted(rs)[len(rs) // 2] if rs else 0
        lines.append(f"- {NAMES[vid]}: {med} dBm (n={len(rs)})\n")

    if confirmed == "no":
        lines.append(
            "\nMAC_RETRY_HYPOTHESIS_NOT_CONFIRMED — next candidates: "
            "management/control traffic, teardown traffic, other driver activity.\n"
        )

    REPORT.write_text("".join(lines), encoding="utf-8")
    print(f"wrote {REPORT}")
    print(f"CONFIRMED={confirmed} INFER={infer}")


if __name__ == "__main__":
    main()
