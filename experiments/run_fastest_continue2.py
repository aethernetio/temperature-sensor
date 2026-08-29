"""Resume from PRE sweep with retry=10 (R0 caused 13/20 on PRE_200)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_fastest_path import (  # noqa: E402
    STATE,
    append_tsv,
    ensure_receiver,
    log,
    pass20,
    run_test,
    write_chat,
)

# Record orphaned PRE_200 retry=0 failure if not already in TSV
RESULTS = Path(__file__).resolve().parent / "prepared_wifi_fastest_path.tsv"
tsv = RESULTS.read_text(encoding="utf-8") if RESULTS.exists() else ""
if "PRE_200\t" not in tsv and "PRE_200_R0FAIL" not in tsv:
    append_tsv(
        "PRE_200_R0FAIL",
        {
            "assoc": 0x1A,
            "auth": 3,
            "pre": 200,
            "post": 300,
            "del": 13,
            "plan": 20,
            "n": 20,
            "conn": 151,
            "cyc": 740,
            "p90": 750,
            "mx": 760,
            "wr": 20,
            "enc": 20,
            "st": 20,
            "cba": 0,
            "cbm": 0,
            "pm": 0,
        },
    )

CUR = {
    "AE_EXP_FAST_PRE_MS": "200",
    "AE_EXP_FAST_POST_MS": "300",
    "AE_EXP_FAST_USE_BSSID": "0",
    "AE_EXP_FAST_FAST_SCAN": "0",
    "AE_EXP_FAST_AUTH": "2",
    "AE_EXP_FAST_RETRY": "10",  # R0 unreliable (13/20)
    "AE_EXP_FAST_POST_MODE": "0",
    "AE_EXP_FAST_AMPDU_TX_OFF": "0",
    "AE_EXP_FAST_STORAGE_RAM": "0",
    "AE_EXP_FAST_DISABLE_WPA3": "1",
}

BEST = {
    "name": "AUTH3B_WPA3SAE_OFF_VERIFIED",
    "cyc": 710,
    "conn": 128,
    "del": 20,
    "plan": 20,
    "pre": 200,
    "post": 300,
    "pm": 0,
    "p90": 810,
    "auth": 3,
}

TEST_NO = 13
REMAINING = 12


def consider(name: str, r: dict) -> None:
    global BEST
    if r["del"] < r["plan"] and r["plan"] <= 20:
        return
    if r["plan"] > 20 and r["del"] < int(0.95 * r["plan"]):
        return
    if BEST is None or r["cyc"] + 20 < BEST["cyc"] or (
        r["cyc"] < BEST["cyc"] and r["del"] >= BEST.get("del", 0)
    ):
        BEST = dict(r)
        BEST["name"] = name


def report(name: str, r: dict, nxt: str) -> None:
    global TEST_NO, REMAINING
    consider(name, r)
    REMAINING = max(0, REMAINING - 1)
    write_chat(TEST_NO, REMAINING, name, r, BEST, nxt)
    TEST_NO += 1
    STATE.write_text(
        json.dumps({"best": BEST, "cur": CUR, "last": name, "result": r}, indent=2),
        encoding="utf-8",
    )


def force_wpa3_off() -> None:
    sdk = Path(
        r"C:\Users\nickc\Projects\temperature-sensor-prepared"
        r"\build-esp32c6-save-bench-smoke\sdkconfig"
    )
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
    from run_fastest_path import cmake_configure

    sdk = Path(
        r"C:\Users\nickc\Projects\temperature-sensor-prepared"
        r"\build-esp32c6-save-bench-smoke\sdkconfig"
    )
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
            **CUR,
            "AE_EXP_FAST_DISABLE_WPA3": "",
            "AE_EXP_FAST_AUTH": "0",
            "AE_EXP_FAST_TEST_ID": "99",
        }
    )
    log("WPA3 SAE restore attempted")


def main() -> int:
    global TEST_NO
    log("=== CONTINUE PRE sweep retry=10 (R0 was unreliable) ===")
    write_chat(
        13,
        12,
        "PRE_200_R0FAIL",
        {
            "del": 13,
            "plan": 20,
            "cyc": 740,
            "conn": 151,
            "p90": 750,
            "pre": 200,
            "post": 300,
            "pm": 0,
            "cbm": 0,
        },
        BEST,
        "PRE_200 with retry=10",
    )
    TEST_NO = 14
    ensure_receiver()
    force_wpa3_off()

    best_pre = 200
    pre_vals = [200, 150, 100, 75, 50, 25, 0]
    for i, pre in enumerate(pre_vals):
        name = f"PRE_{pre}"
        rr = run_test(
            name,
            {**CUR, "AE_EXP_FAST_TEST_ID": str(20 + i), "AE_EXP_FAST_PRE_MS": str(pre)},
            20,
            timeout_s=900,
        )
        nxt = f"PRE_{pre_vals[i+1]}" if i + 1 < len(pre_vals) else "POST sweep"
        if rr["del"] >= 20:
            report(name, rr, nxt)
            best_pre = pre
            continue
        if rr["del"] == 19:
            report(name, rr, name + " repeat")
            rr2 = run_test(
                name + "_R",
                {
                    **CUR,
                    "AE_EXP_FAST_TEST_ID": str(40 + i),
                    "AE_EXP_FAST_PRE_MS": str(pre),
                },
                20,
                timeout_s=900,
            )
            if rr2["del"] >= 19:
                report(name + "_R", rr2, nxt)
                best_pre = pre
                continue
            report(name + "_R", rr2, f"PRE stop; best_pre={best_pre}")
            break
        report(name, rr, f"PRE stop at {pre}; best_pre={best_pre}")
        log(f"PRE {pre} too aggressive — stop")
        break
    CUR["AE_EXP_FAST_PRE_MS"] = str(best_pre)

    best_post = 300
    post_vals = [300, 250, 200, 150, 100, 75, 50, 25, 0]
    for i, post in enumerate(post_vals):
        name = f"POST_{post}"
        rr = run_test(
            name,
            {
                **CUR,
                "AE_EXP_FAST_TEST_ID": str(50 + i),
                "AE_EXP_FAST_POST_MS": str(post),
            },
            20,
            timeout_s=900,
        )
        nxt = f"POST_{post_vals[i+1]}" if i + 1 < len(post_vals) else "2D"
        if rr["del"] >= 20:
            report(name, rr, nxt)
            best_post = post
            continue
        if rr["del"] == 19:
            report(name, rr, name + " repeat")
            rr2 = run_test(
                name + "_R",
                {
                    **CUR,
                    "AE_EXP_FAST_TEST_ID": str(60 + i),
                    "AE_EXP_FAST_POST_MS": str(post),
                },
                20,
                timeout_s=900,
            )
            if rr2["del"] >= 19:
                report(name + "_R", rr2, nxt)
                best_post = post
                continue
            report(name + "_R", rr2, f"POST stop; best_post={best_post}")
            break
        report(name, rr, f"POST stop; best_post={best_post}")
        break
    CUR["AE_EXP_FAST_POST_MS"] = str(best_post)

    p = int(CUR["AE_EXP_FAST_PRE_MS"])
    q = int(CUR["AE_EXP_FAST_POST_MS"])
    for i, (pp, qq) in enumerate(
        [
            (max(0, p - 25), q + 25),
            (p + 25, max(0, q - 25)),
            (max(0, p - 50), q + 50),
            (p + 50, max(0, q - 50)),
        ]
    ):
        name = f"2D_p{pp}_q{qq}"
        rr = run_test(
            name,
            {
                **CUR,
                "AE_EXP_FAST_TEST_ID": str(70 + i),
                "AE_EXP_FAST_PRE_MS": str(pp),
                "AE_EXP_FAST_POST_MS": str(qq),
            },
            20,
            timeout_s=900,
        )
        report(name, rr, "next 2D or VAL100")
        if pass20(rr) and BEST and BEST.get("name") == name:
            CUR["AE_EXP_FAST_PRE_MS"] = str(pp)
            CUR["AE_EXP_FAST_POST_MS"] = str(qq)

    if BEST:
        CUR["AE_EXP_FAST_PRE_MS"] = str(BEST.get("pre", CUR["AE_EXP_FAST_PRE_MS"]))
        CUR["AE_EXP_FAST_POST_MS"] = str(BEST.get("post", CUR["AE_EXP_FAST_POST_MS"]))

    log(f"validation flags={CUR}")
    v100 = run_test(
        "VAL100", {**CUR, "AE_EXP_FAST_TEST_ID": "80"}, 100, timeout_s=45 * 100 + 600
    )
    report("VAL100", v100, "VAL200 or SAFE")
    if v100["del"] < 98:
        cur2 = dict(CUR)
        cur2["AE_EXP_FAST_PRE_MS"] = "200"
        cur2["AE_EXP_FAST_POST_MS"] = "300"
        v100b = run_test(
            "VAL100_SAFE",
            {**cur2, "AE_EXP_FAST_TEST_ID": "81"},
            100,
            timeout_s=45 * 100 + 600,
        )
        report("VAL100_SAFE", v100b, "VAL200")
        if v100b["del"] > v100["del"]:
            CUR.update(cur2)
            v100 = v100b
    if v100["del"] == 98:
        v100r = run_test(
            "VAL100_REPEAT",
            {**CUR, "AE_EXP_FAST_TEST_ID": "82"},
            100,
            timeout_s=45 * 100 + 600,
        )
        report("VAL100_REPEAT", v100r, "VAL200")

    v200 = run_test(
        "VAL200", {**CUR, "AE_EXP_FAST_TEST_ID": "90"}, 200, timeout_s=45 * 200 + 600
    )
    report("VAL200", v200, "D1 AMPDU")

    d1 = run_test(
        "D1_AMPDU_TX_OFF",
        {**CUR, "AE_EXP_FAST_TEST_ID": "91", "AE_EXP_FAST_AMPDU_TX_OFF": "1"},
        20,
        timeout_s=900,
    )
    report("D1_AMPDU_TX_OFF", d1, "STORAGE_RAM")

    ram = run_test(
        "STORAGE_RAM",
        {**CUR, "AE_EXP_FAST_TEST_ID": "92", "AE_EXP_FAST_STORAGE_RAM": "1"},
        20,
        timeout_s=900,
    )
    report("STORAGE_RAM", ram, "report + restore WPA3")

    # LWIP IRAM A/B via sdkconfig patch if needed — deferred to report writer
    log("=== SCREENING COMPLETE ===")
    log(f"BEST {BEST}")
    log(f"VAL200 {v200}")
    STATE.write_text(
        json.dumps({"best": BEST, "cur": CUR, "val200": v200}, indent=2),
        encoding="utf-8",
    )
    restore_wpa3()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        log(f"FATAL {ex}")
        raise
