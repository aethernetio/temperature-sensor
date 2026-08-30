"""Analyze prepared_ap_aethernetio_3x10.tsv and write short report."""

from __future__ import annotations

import csv
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
TSV = ROOT / "experiments" / "prepared_ap_aethernetio_3x10.tsv"
RX_LOG = ROOT / "experiments" / "prepared_ap_aethernetio_3x10_rx.log"
REPORT = ROOT / "experiments" / "PREPARED_AP_AETHERNETIO_3X10_REPORT.md"

WIFI_REASON = {
    2: "AUTH_EXPIRE",
    15: "4WAY_HANDSHAKE_TIMEOUT",
    201: "NO_AP_FOUND",
    204: "HANDSHAKE_TIMEOUT",
    205: "CONNECTION_FAIL",
    210: "ASSOC_FAIL",
    211: "ASSOC_COMEBACK_TIME_TOO_LONG",
    212: "ASSOC_REFUSED_TEMPORARILY",
}


def iu(x: str) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def pct(vals: list[int], q: int) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if q <= 0:
        return float(s[0])
    if q >= 100:
        return float(s[-1])
    k = (len(s) - 1) * q / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] + (s[c] - s[f]) * (k - f)


def ms(us: float) -> str:
    return f"{us / 1000.0:.1f}"


def main() -> None:
    rows = (
        list(csv.DictReader(TSV.open(encoding="utf-8"), delimiter="\t"))
        if TSV.exists()
        else []
    )
    fulls = [r for r in rows if iu(r["kind"]) == 1]
    hots = [r for r in rows if iu(r["kind"]) == 2]

    full_user = [iu(r["user_us"]) for r in fulls]
    hot_user = [iu(r["user_us"]) for r in hots]
    hot_wifi = [iu(r["wifi_us"]) for r in hots]
    connect = [iu(r["connect_us"]) for r in hots if iu(r["connect_us"]) > 0]
    txdone = [iu(r["txdone_us"]) for r in hots if iu(r["txdone_us"]) > 0]
    wake = [iu(r["sleep_overhead_us"]) for r in hots if iu(r["sleep_overhead_us"]) > 0]
    rssi = [iu(r["rssi"]) for r in hots if r.get("rssi", "0") not in ("", "0")]
    channels = [iu(r["actual_channel"]) for r in hots if iu(r.get("actual_channel", "0")) > 0]

    cb_seen = sum(1 for r in hots if iu(r.get("cb_seen", 0)))
    cb_to = sum(1 for r in hots if iu(r.get("cb_timeout", 0)))
    brownouts = sum(1 for r in rows if iu(r.get("brownout", 0)))

    disc_reasons = Counter(
        iu(r.get("last_disconnect_reason", 0))
        for r in hots
        if iu(r.get("disconnect_count", 0)) > 0
    )

    hot_recv = 0
    full_recv = 0
    if RX_LOG.exists():
        import re

        t = RX_LOG.read_text(encoding="utf-8", errors="replace")
        hot_recv = len(re.findall(r"^DS_HOT ", t, re.M))
        full_recv = len(set(re.findall(r"^DS_FULL outer=(\d+)", t, re.M)))

    connect_raw = [iu(r["connect_us"]) for r in hots]

    failed_assoc = 0
    if RX_LOG.exists():
        import re

        for m in re.finditer(r"failed_assoc_wakes=(\d+)", RX_LOG.read_text(encoding="utf-8")):
            failed_assoc = max(failed_assoc, int(m.group(1)))

    # Baseline from chirkov D1 incomplete + prior deepsleep refs
    baseline_hot_user = 250.3
    baseline_connect = 157.7
    baseline_delivery = 249 / 250 * 100

    delivery_pct = (hot_recv / 30.0 * 100) if hot_recv else 0.0
    hot_med = pct(hot_user, 50)
    connect_med = pct(connect, 50) if connect else 0.0

    lines = [
        "# Prepared AP aethernetio 3×10 Report",
        "",
        "## CONFIG",
        "",
        "- AP SSID: **aethernetio**",
        "- WPA2, Wi-Fi 4 b/g/n",
        "- cached channel yes, BSSID no",
        "- static IP / static ARP yes",
        "- PRE 25 ms, TX-done callback, POST 0",
        "- WIFI_STORAGE_RAM yes (D1)",
        "- wifi nvs ON",
        "- TX power default, MAC retry default",
        "- external RTC crystal, CAL cycles 1024",
        "- deep sleep **1 s** between HOT/FULL",
        "- 3 FULL × 10 HOT",
        "",
        "## RESULT",
        "",
        f"- FULL received: **{full_recv}/3**",
        f"- FULL user raw (ms): {[ms(x) for x in full_user] if full_user else '[]'}",
        f"- FULL user median: **{ms(pct(full_user, 50)) if full_user else 'n/a'} ms**",
        f"- HOT sendto (TSV): **{len(hots)}/30**",
        f"- HOT received: **{hot_recv}/30** ({delivery_pct:.1f}%)",
        "",
        "### HOT timing",
        "",
        f"- user median/p90/max: **{ms(pct(hot_user, 50))} / {ms(pct(hot_user, 90))} / {ms(pct(hot_user, 99))} / {ms(max(hot_user) if hot_user else 0)} ms**",
        f"- Wi-Fi median: **{ms(pct(hot_wifi, 50))} ms**",
        f"- connect median/p90/max: **{ms(connect_med)} / {ms(pct(connect, 90)) if connect else 'n/a'} / {ms(max(connect) if connect else 0)} ms**",
        f"- tx-done median: **{ms(pct(txdone, 50)) if txdone else 'n/a'} ms**",
        f"- wake overhead median: **{ms(pct(wake, 50)) if wake else 'n/a'} ms**",
        "",
        "### Wi-Fi / association",
        "",
        f"- channel(s): {sorted(set(channels)) if channels else 'n/a'}",
        f"- RSSI median: **{int(statistics.median(rssi)) if rssi else 'n/a'} dBm**",
        f"- failed association wakes: **{failed_assoc}**",
        f"- callback seen/timeouts: **{cb_seen}/{cb_to}**",
        f"- brownouts: **{brownouts}**",
        "",
        "### Connect times (all HOT, ms)",
        "",
        f"`{[ms(x) for x in connect_raw]}`",
        "",
        "### Disconnect reasons (when disconnect_count>0)",
        "",
    ]
    if disc_reasons:
        for code, cnt in sorted(disc_reasons.items()):
            name = WIFI_REASON.get(code, f"REASON_{code}")
            lines.append(f"- {name} ({code}): {cnt}")
    else:
        lines.append("- none recorded")

    lines += [
        "",
        "## COMPARE vs previous AP (chirkov baseline refs)",
        "",
        f"| metric | chirkov ref | aethernetio | delta |",
        f"|--------|-------------|-------------|-------|",
        f"| delivery | {baseline_delivery:.1f}% | {delivery_pct:.1f}% | {delivery_pct - baseline_delivery:+.1f} pp |",
        f"| hot user med | {baseline_hot_user:.1f} ms | {hot_med/1000:.1f} ms | {(hot_med - baseline_hot_user*1000)/1000:+.1f} ms |",
        f"| connect med | {baseline_connect:.1f} ms | {connect_med/1000:.1f} ms | {(connect_med - baseline_connect*1000)/1000:+.1f} ms |",
        f"| failed assoc | 0 | {failed_assoc} | — |",
        "",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT}")


if __name__ == "__main__":
    main()
