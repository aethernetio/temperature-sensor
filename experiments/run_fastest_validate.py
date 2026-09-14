"""VAL100/200 + AMPDU/STORAGE + report for fastest-path campaign."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_fastest_path import (  # noqa: E402
    RESULTS,
    STATE,
    cmake_configure,
    ensure_receiver,
    log,
    run_test,
    write_chat,
)

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")

# Primary: POST_150_R (reliable PRE=200). Secondary: 2D_p150_q200.
PRIMARY = {
    "AE_EXP_FAST_PRE_MS": "200",
    "AE_EXP_FAST_POST_MS": "150",
    "AE_EXP_FAST_USE_BSSID": "0",
    "AE_EXP_FAST_FAST_SCAN": "0",
    "AE_EXP_FAST_AUTH": "2",
    "AE_EXP_FAST_RETRY": "10",
    "AE_EXP_FAST_POST_MODE": "0",
    "AE_EXP_FAST_AMPDU_TX_OFF": "0",
    "AE_EXP_FAST_STORAGE_RAM": "0",
    "AE_EXP_FAST_DISABLE_WPA3": "1",
}
SECONDARY = {**PRIMARY, "AE_EXP_FAST_PRE_MS": "150", "AE_EXP_FAST_POST_MS": "200"}

BEST = {
    "name": "POST_150_R",
    "cyc": 550,
    "conn": 125,
    "del": 20,
    "plan": 20,
    "pre": 200,
    "post": 150,
    "pm": 0,
    "p90": 580,
    "auth": 3,
}

TEST_NO = 26
REMAINING = 8


def force_wpa3_off() -> None:
    sdk = ROOT / "build-esp32c6-save-bench-smoke" / "sdkconfig"
    text = sdk.read_text(encoding="utf-8")
    text = text.replace(
        "CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y",
        "# CONFIG_ESP_WIFI_ENABLE_WPA3_SAE is not set",
    )
    text = text.replace(
        "CONFIG_ESP32_WIFI_ENABLE_WPA3_SAE=y",
        "# CONFIG_ESP32_WIFI_ENABLE_WPA3_SAE is not set",
    )
    sdk.write_text(text, encoding="utf-8")


def restore_wpa3() -> None:
    sdk = ROOT / "build-esp32c6-save-bench-smoke" / "sdkconfig"
    text = sdk.read_text(encoding="utf-8")
    text = text.replace(
        "# CONFIG_ESP_WIFI_ENABLE_WPA3_SAE is not set",
        "CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y",
    )
    text = text.replace(
        "# CONFIG_ESP32_WIFI_ENABLE_WPA3_SAE is not set",
        "CONFIG_ESP32_WIFI_ENABLE_WPA3_SAE=y",
    )
    if "CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y" not in text:
        text += "\nCONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y\n"
    sdk.write_text(text, encoding="utf-8")
    cmake_configure(
        {
            **PRIMARY,
            "AE_EXP_FAST_DISABLE_WPA3": "",
            "AE_EXP_FAST_AUTH": "0",
            "AE_EXP_FAST_TEST_ID": "99",
        }
    )
    log("WPA3 SAE restore attempted")


def report(name: str, r: dict, nxt: str, best: dict) -> dict:
    global TEST_NO, REMAINING, BEST
    if r["del"] == r["plan"] and (
        r["cyc"] + 20 < best["cyc"]
        or (r["plan"] >= 100 and r["del"] >= int(0.99 * r["plan"]) and r["cyc"] < best["cyc"])
    ):
        BEST = dict(r)
        BEST["name"] = name
        best = BEST
    REMAINING = max(0, REMAINING - 1)
    write_chat(TEST_NO, REMAINING, name, r, best, nxt)
    TEST_NO += 1
    STATE.write_text(
        json.dumps({"best": best, "last": name, "result": r}, indent=2),
        encoding="utf-8",
    )
    return best


def write_final_report(winner_flags: dict, v200: dict, extras: dict) -> None:
    report_md = ROOT / "experiments" / "PREPARED_WIFI_FASTEST_PATH_REPORT.md"
    tsv = RESULTS.read_text(encoding="utf-8") if RESULTS.exists() else ""
    old = 850
    new = v200.get("cyc", BEST["cyc"])
    saving = old - new
    pct = 100.0 * saving / old if old else 0
    speedup = old / new if new else 0
    body = f"""# Prepared Wi-Fi Fastest Path Report (ESP32-C6)

## Pins
- temperature-sensor branch: `thermometer-prepared-send-v0`
- aether-client-cpp: `157aadbec8e7b852d0f89274307ff7cb8103e5f7` **unchanged=yes**
- ESP-IDF: v6.0.2
- Silent Release, WIFI_PS_NONE, Wi-Fi 4 only, auto PHY, max TX power
- No sleep/reboot during benchmark; 1 s gap outside timer

## OLD vs NEW
- OLD: ~{old} ms static-IP cycle (prior C6/C8)
- NEW: **{new} ms** (VAL200 median)
- Absolute saving: **{saving} ms**
- Percent saving: **{pct:.1f}%**
- Speedup: **{speedup:.2f}x**

## BEST RELIABLE CONFIG (measurement winner)
- Wi-Fi protocol: 802.11b/g/n only
- Channel cache: yes
- BSSID cache: **no**
- Static IP + netmask + gateway: yes
- Static ARP (gateway MAC): yes
- Scan method: default (not WIFI_FAST_SCAN)
- WPA mode: **WPA2-PSK** (`authmode=3`) via benchmark-only `CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=n`
  - Production security decision deferred; do **not** auto-weaken production
- SAE method: N/A (WPA2)
- Association retry max: **10** (R0/R1 unreliable on delivery)
- PRE: **{winner_flags.get('AE_EXP_FAST_PRE_MS')} ms**
- POST: **{winner_flags.get('AE_EXP_FAST_POST_MS')} ms** fixed hold (callback not usable)
- AMPDU TX: already off in sdkconfig; D1 explicit off = {extras.get('ampdu','n/a')}
- Wi-Fi storage: NVS default; STORAGE_RAM = {extras.get('ram','n/a')}
- IRAM: ESP_WIFI_IRAM_OPT/RX_IRAM already on; LWIP_IRAM was off (see notes)

## Callback verdict
- API: `esp_wifi_set_tx_done_cb` (IDF 6.0.2 `esp_private/wifi.h`)
- CB0: callback **fires** (cb_any=18/20) but **fingerprint match=0**; delivery 18/20
- Verdict: **CALLBACK_NOT_USABLE** for normal lwIP UDP (cannot reliably bind to our frame)

## Auth results
- AUTH1 WPA3 BOTH: negotiated authmode=7 (WPA2/WPA3 transition AP info), ~840 ms
- AUTH2 H2E-only: authmode still 7, delivery 18/20
- AUTH3 WPA2 threshold only: still authmode=7 (ESP still picks WPA3 path)
- AUTH3B WPA3 SAE compile-disabled: **authmode=3 (WPA2_PSK)**, connect ~128 ms, cycle ~710 ms @ PRE/POST 200/300

## Association extras (on WPA3 BASE)
- A1 BSSID / A2 FAST_SCAN / A3 both: no clear ≥20 ms win; some delivery loss

## Delay search
- PRE: 200 reliable; 150 too aggressive with POST=300 (14/20)
- POST: 150 reliable (20/20 after repeat); 100 too aggressive (13/20)
- 2D: `PRE=150,POST=200` also 20/20 @ 540 ms screen (secondary candidate)

## Validation
- VAL100 primary: {extras.get('v100_primary')}
- VAL100 secondary: {extras.get('v100_secondary')}
- VAL200 winner: **{v200.get('del')}/{v200.get('plan')}** median_cycle={v200.get('cyc')} connect={v200.get('conn')} p90={v200.get('p90')} max={v200.get('mx')}

## Production note
`SendPreparedOnce` **not** switched to winner automatically.

## TSV
See `experiments/prepared_wifi_fastest_path.tsv`.

### Raw TSV snapshot
```
{tsv}
```
"""
    report_md.write_text(body, encoding="utf-8")
    log(f"wrote {report_md}")


def main() -> int:
    global BEST
    log("=== VAL + AMPDU + REPORT ===")
    ensure_receiver()
    force_wpa3_off()

    v100a = run_test(
        "VAL100_PRIMARY",
        {**PRIMARY, "AE_EXP_FAST_TEST_ID": "80"},
        100,
        timeout_s=45 * 100 + 600,
    )
    BEST = report("VAL100_PRIMARY", v100a, "VAL100 secondary", BEST)

    v100b = run_test(
        "VAL100_SECONDARY",
        {**SECONDARY, "AE_EXP_FAST_TEST_ID": "81"},
        100,
        timeout_s=45 * 100 + 600,
    )
    BEST = report("VAL100_SECONDARY", v100b, "pick winner VAL200", BEST)

    winner = PRIMARY
    winner_name = "PRIMARY_PRE200_POST150"
    # Prefer >=99/100; if secondary much faster and >=98, prefer it
    if v100b["del"] >= 99 and (
        v100b["cyc"] + 20 < v100a["cyc"] or v100b["del"] > v100a["del"]
    ):
        winner = SECONDARY
        winner_name = "SECONDARY_PRE150_POST200"
    elif v100a["del"] < 98 and v100b["del"] > v100a["del"]:
        winner = SECONDARY
        winner_name = "SECONDARY_PRE150_POST200"
    elif v100a["del"] == 98:
        v100ar = run_test(
            "VAL100_PRIMARY_R",
            {**PRIMARY, "AE_EXP_FAST_TEST_ID": "82"},
            100,
            timeout_s=45 * 100 + 600,
        )
        BEST = report("VAL100_PRIMARY_R", v100ar, "VAL200", BEST)
        v100a = v100ar

    log(f"winner for VAL200: {winner_name} {winner}")
    v200 = run_test(
        "VAL200",
        {**winner, "AE_EXP_FAST_TEST_ID": "90"},
        200,
        timeout_s=45 * 200 + 900,
    )
    BEST = report("VAL200", v200, "D1 AMPDU", BEST)

    d1 = run_test(
        "D1_AMPDU_TX_OFF",
        {**winner, "AE_EXP_FAST_TEST_ID": "91", "AE_EXP_FAST_AMPDU_TX_OFF": "1"},
        20,
        timeout_s=900,
    )
    BEST = report("D1_AMPDU_TX_OFF", d1, "STORAGE_RAM", BEST)

    ram = run_test(
        "STORAGE_RAM",
        {**winner, "AE_EXP_FAST_TEST_ID": "92", "AE_EXP_FAST_STORAGE_RAM": "1"},
        20,
        timeout_s=900,
    )
    BEST = report("STORAGE_RAM", ram, "write report", BEST)

    extras = {
        "v100_primary": f"{v100a['del']}/{v100a['plan']} cyc={v100a['cyc']}",
        "v100_secondary": f"{v100b['del']}/{v100b['plan']} cyc={v100b['cyc']}",
        "ampdu": f"{d1['del']}/{d1['plan']} cyc={d1['cyc']}",
        "ram": f"{ram['del']}/{ram['plan']} cyc={ram['cyc']}",
    }
    write_final_report(winner, v200, extras)
    STATE.write_text(
        json.dumps(
            {
                "best": BEST,
                "winner": winner,
                "winner_name": winner_name,
                "val200": v200,
                "extras": extras,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    restore_wpa3()
    log("=== ALL DONE ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        log(f"FATAL {ex}")
        raise
