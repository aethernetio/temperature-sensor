"""Safer validation after both aggressive VAL100s failed <98/100."""

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

BASE_AUTH = {
    "AE_EXP_FAST_USE_BSSID": "0",
    "AE_EXP_FAST_FAST_SCAN": "0",
    "AE_EXP_FAST_AUTH": "2",
    "AE_EXP_FAST_RETRY": "10",
    "AE_EXP_FAST_POST_MODE": "0",
    "AE_EXP_FAST_AMPDU_TX_OFF": "0",
    "AE_EXP_FAST_STORAGE_RAM": "0",
    "AE_EXP_FAST_DISABLE_WPA3": "1",
}

# Candidates ordered: safer delays first after failed aggressive VAL100s
CANDIDATES = [
    ("SAFE_200_300", {**BASE_AUTH, "AE_EXP_FAST_PRE_MS": "200", "AE_EXP_FAST_POST_MS": "300"}),
    ("MID_200_200", {**BASE_AUTH, "AE_EXP_FAST_PRE_MS": "200", "AE_EXP_FAST_POST_MS": "200"}),
    ("FAST_200_150", {**BASE_AUTH, "AE_EXP_FAST_PRE_MS": "200", "AE_EXP_FAST_POST_MS": "150"}),
    ("FAST_150_200", {**BASE_AUTH, "AE_EXP_FAST_PRE_MS": "150", "AE_EXP_FAST_POST_MS": "200"}),
]

BEST = {
    "name": "POST_150_R",
    "cyc": 550,
    "conn": 125,
    "del": 20,
    "plan": 20,
    "pre": 200,
    "post": 150,
    "pm": 0,
}

TEST_NO = 28
REMAINING = 10


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
            **BASE_AUTH,
            "AE_EXP_FAST_DISABLE_WPA3": "",
            "AE_EXP_FAST_AUTH": "0",
            "AE_EXP_FAST_PRE_MS": "200",
            "AE_EXP_FAST_POST_MS": "300",
            "AE_EXP_FAST_TEST_ID": "99",
        }
    )
    log("WPA3 SAE restore attempted")


def report(name: str, r: dict, nxt: str) -> None:
    global TEST_NO, REMAINING, BEST
    REMAINING = max(0, REMAINING - 1)
    write_chat(TEST_NO, REMAINING, name, r, BEST, nxt)
    TEST_NO += 1
    STATE.write_text(
        json.dumps({"best": BEST, "last": name, "result": r}, indent=2),
        encoding="utf-8",
    )


def write_report(winner_name: str, flags: dict, v100s: list, v200: dict, d1: dict, ram: dict) -> None:
    tsv = RESULTS.read_text(encoding="utf-8") if RESULTS.exists() else ""
    old, new = 850, v200.get("cyc", 0)
    saving = old - new
    pct = 100.0 * saving / old if old else 0
    speedup = (old / new) if new else 0
    v100_lines = "\n".join(
        f"- {n}: {r['del']}/{r['plan']} cyc={r['cyc']} conn={r['conn']} p90={r['p90']}"
        for n, r in v100s
    )
    md = f"""# Prepared Wi-Fi Fastest Path Report (ESP32-C6)

## Pins
- temperature-sensor branch: `thermometer-prepared-send-v0`
- aether-client-cpp: `157aadbec8e7b852d0f89274307ff7cb8103e5f7` **unchanged=yes**
- ESP-IDF v6.0.2 · Silent Release · WIFI_PS_NONE · Wi-Fi 4 · auto PHY · max TX
- No sleep/reboot in bench; 1 s gap outside timer

## OLD vs NEW
- OLD: ~{old} ms (prior C6/C8 static-IP cycle)
- NEW: **{new} ms** (VAL200 median cycle)
- Absolute saving: **{saving} ms**
- Percent saving: **{pct:.1f}%**
- Speedup: **{speedup:.2f}x**

## BEST RELIABLE CONFIG (measurement winner: {winner_name})
- Protocol: 802.11b/g/n only
- Channel cache: yes · BSSID: **no** · Static IP: yes · Static ARP: yes
- Scan: default (not FAST_SCAN)
- Auth: **WPA2-PSK** (negotiated authmode=3) via benchmark-only `CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=n`
  - Security decision deferred; production not auto-weakened
- Retry max: **10**
- PRE: **{flags['AE_EXP_FAST_PRE_MS']} ms**
- POST: **{flags['AE_EXP_FAST_POST_MS']} ms** fixed (callback not usable)
- AMPDU TX: sdkconfig already off; D1={d1['del']}/{d1['plan']} cyc={d1['cyc']}
- STORAGE_RAM: {ram['del']}/{ram['plan']} cyc={ram['cyc']}
- IRAM: WIFI IRAM opts already on; LWIP_IRAM was off (not A/B'd if sendto not bottleneck)

## Callback verdict
- `esp_wifi_set_tx_done_cb` fires (cb_any high) but **cb_match=0**; delivery 18/20
- **CALLBACK_NOT_USABLE** for normal lwIP UDP

## Auth / association summary
- BSSID / FAST_SCAN: no clear ≥20 ms win
- WPA3 BOTH / H2E: authmode stays 7 on transition AP
- WPA2 threshold alone: still authmode 7
- WPA3 SAE compile-off: authmode **3**, connect ~128 ms @ 200/300

## VAL100 candidates
{v100_lines}

## VAL200
- delivered **{v200['del']}/{v200['plan']}**
- connect median={v200['conn']} ms · cycle median={v200['cyc']} ms · p90={v200['p90']} · max={v200['mx']}

## Production
`SendPreparedOnce` **not** switched to winner.

## TSV
`experiments/prepared_wifi_fastest_path.tsv`

```
{tsv}
```
"""
    path = ROOT / "experiments" / "PREPARED_WIFI_FASTEST_PATH_REPORT.md"
    path.write_text(md, encoding="utf-8")
    log(f"wrote {path}")


def main() -> int:
    global BEST
    log("=== SAFE/MID VAL cascade after 94% and 97% ===")
    ensure_receiver()
    force_wpa3_off()

    v100s: list[tuple[str, dict]] = [
        ("VAL100_PRIMARY_200_150", {"del": 94, "plan": 100, "cyc": 560, "conn": 131, "p90": 620}),
        ("VAL100_SECONDARY_150_200", {"del": 97, "plan": 100, "cyc": 560, "conn": 131, "p90": 610}),
    ]

    winner_flags = None
    winner_name = None
    winner_r = None

    for i, (name, flags) in enumerate(CANDIDATES):
        if winner_flags is not None and winner_r and winner_r.get("del", 0) >= 99:
            break
        # Prefer safer delays first; only try FAST_* if SAFE/MID did not reach 99
        if name.startswith("FAST_") and any(
            x[0].startswith("VAL100_SAFE") or x[0].startswith("VAL100_MID")
            for x in v100s
        ):
            safe_mid = [x for x in v100s if "SAFE" in x[0] or "MID" in x[0]]
            if safe_mid and max(x[1]["del"] for x in safe_mid) >= 99:
                continue
        tid = 83 + i
        r = run_test(
            f"VAL100_{name}",
            {**flags, "AE_EXP_FAST_TEST_ID": str(tid)},
            100,
            timeout_s=45 * 100 + 600,
        )
        report(f"VAL100_{name}", r, "next candidate or VAL200")
        v100s.append((f"VAL100_{name}", r))
        if r["del"] >= 99:
            winner_flags = flags
            winner_name = name
            winner_r = r
            BEST = {
                **r,
                "name": name,
                "pre": int(flags["AE_EXP_FAST_PRE_MS"]),
                "post": int(flags["AE_EXP_FAST_POST_MS"]),
            }
            break
        if r["del"] >= 98:
            rr = run_test(
                f"VAL100_{name}_R",
                {**flags, "AE_EXP_FAST_TEST_ID": str(tid + 10)},
                100,
                timeout_s=45 * 100 + 600,
            )
            report(f"VAL100_{name}_R", rr, "next")
            v100s.append((f"VAL100_{name}_R", rr))
            if rr["del"] >= 98:
                winner_flags = flags
                winner_name = name
                winner_r = rr
                BEST = {
                    **rr,
                    "name": name,
                    "pre": int(flags["AE_EXP_FAST_PRE_MS"]),
                    "post": int(flags["AE_EXP_FAST_POST_MS"]),
                }
                if rr["del"] >= 99:
                    break
                # 98/98 — keep searching for better delivery unless last

    if winner_flags is None:
        # fall back to best delivery among all v100s
        best_del = -1
        for n, r in v100s:
            if r["del"] > best_del or (r["del"] == best_del and r["cyc"] < (winner_r or {}).get("cyc", 10**9)):
                best_del = r["del"]
                winner_r = r
                winner_name = n
        # map name to flags
        if "200_300" in (winner_name or "") or "SAFE" in (winner_name or ""):
            winner_flags = CANDIDATES[0][1]
        elif "200_200" in (winner_name or "") or "MID" in (winner_name or ""):
            winner_flags = CANDIDATES[1][1]
        elif "200_150" in (winner_name or ""):
            winner_flags = CANDIDATES[2][1]
        else:
            winner_flags = CANDIDATES[3][1]
        log(f"no >=99/100; falling back to best delivery {winner_name} {best_del}/100")

    log(f"VAL200 winner={winner_name} flags={winner_flags}")
    v200 = run_test(
        "VAL200",
        {**winner_flags, "AE_EXP_FAST_TEST_ID": "90"},
        200,
        timeout_s=45 * 200 + 900,
    )
    report("VAL200", v200, "D1 AMPDU")
    BEST = {
        **v200,
        "name": winner_name,
        "pre": int(winner_flags["AE_EXP_FAST_PRE_MS"]),
        "post": int(winner_flags["AE_EXP_FAST_POST_MS"]),
    }

    d1 = run_test(
        "D1_AMPDU_TX_OFF",
        {**winner_flags, "AE_EXP_FAST_TEST_ID": "91", "AE_EXP_FAST_AMPDU_TX_OFF": "1"},
        20,
        timeout_s=900,
    )
    report("D1_AMPDU_TX_OFF", d1, "STORAGE_RAM")

    ram = run_test(
        "STORAGE_RAM",
        {**winner_flags, "AE_EXP_FAST_TEST_ID": "92", "AE_EXP_FAST_STORAGE_RAM": "1"},
        20,
        timeout_s=900,
    )
    report("STORAGE_RAM", ram, "write report + restore WPA3")

    write_report(winner_name or "?", winner_flags, v100s, v200, d1, ram)
    STATE.write_text(
        json.dumps(
            {
                "best": BEST,
                "winner_name": winner_name,
                "winner_flags": winner_flags,
                "val200": v200,
                "v100s": [(n, r) for n, r in v100s],
            },
            indent=2,
            default=str,
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
