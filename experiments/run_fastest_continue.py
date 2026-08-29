"""Resume fastest-path campaign after AUTH3B WPA2 SAE-off verified win."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_fastest_path import (  # noqa: E402
    CHAT,
    RESULTS,
    STATE,
    cmake_configure,
    ensure_receiver,
    log,
    ninja_build,
    pass20,
    run_test,
    write_chat,
)

# Winner association+auth from AUTH3B verified: BASE caches + real WPA2
# (CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=n). Security decision later; report only.
CUR = {
    "AE_EXP_FAST_PRE_MS": "200",
    "AE_EXP_FAST_POST_MS": "300",
    "AE_EXP_FAST_USE_BSSID": "0",
    "AE_EXP_FAST_FAST_SCAN": "0",
    "AE_EXP_FAST_AUTH": "2",
    "AE_EXP_FAST_RETRY": "10",
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

TEST_NO = 9
REMAINING = 14


def consider(name: str, r: dict) -> None:
    global BEST
    if not pass20(r) and r.get("plan", 20) <= 20 and r["del"] < r["plan"]:
        return
    if r["del"] < max(1, int(0.95 * r["plan"])):
        return
    clear = BEST is None or r["cyc"] + 20 < BEST["cyc"]
    if clear or (BEST and r["cyc"] < BEST["cyc"] and r["del"] >= BEST["del"]):
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
    """Ensure sdkconfig.defaults.wpa2only is applied via cmake flag."""
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
    # re-patch after cmake may re-enable from defaults without overlay
    text = sdk.read_text(encoding="utf-8")
    if "CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y" not in text:
        text = text.replace(
            "# CONFIG_ESP_WIFI_ENABLE_WPA3_SAE is not set",
            "CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y",
        )
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
    log("=== FASTEST PATH CONTINUE from R0 ===")
    ensure_receiver()
    force_wpa3_off()

    # 6. Retry
    r0 = run_test("R0", {**CUR, "AE_EXP_FAST_TEST_ID": "8", "AE_EXP_FAST_RETRY": "0"}, 20)
    report("R0", r0, "R1 retry=1 x20")
    r1 = run_test("R1", {**CUR, "AE_EXP_FAST_TEST_ID": "9", "AE_EXP_FAST_RETRY": "1"}, 20)
    report("R1", r1, "R3 retry=3 x20")
    r3 = run_test("R3", {**CUR, "AE_EXP_FAST_TEST_ID": "10", "AE_EXP_FAST_RETRY": "3"}, 20)
    report("R3", r3, "CB0 tx-done callback POST=0 x20")

    retry = 10
    if pass20(r0) and r0["wr"] >= 19:
        retry = 0
    elif pass20(r1) and r1["wr"] >= 19:
        retry = 1
    elif pass20(r3) and r3["wr"] >= 19:
        retry = 3
    # Prefer smallest retry that keeps delivery; don't pick by median alone
    CUR["AE_EXP_FAST_RETRY"] = str(retry)
    log(f"retry selected {retry}")

    # 7. Callback
    cb0 = run_test(
        "CB0",
        {
            **CUR,
            "AE_EXP_FAST_TEST_ID": "11",
            "AE_EXP_FAST_POST_MODE": "1",
            "AE_EXP_FAST_POST_MS": "0",
        },
        20,
    )
    cb_usable = pass20(cb0) and cb0.get("cbm", 0) >= 15
    if not cb_usable:
        log("CALLBACK_NOT_USABLE")
        CUR["AE_EXP_FAST_POST_MODE"] = "0"
        CUR["AE_EXP_FAST_POST_MS"] = "300"
        report("CB0", cb0, "PRE sweep from 200ms (fixed POST=300)")
    else:
        CUR["AE_EXP_FAST_POST_MODE"] = "1"
        CUR["AE_EXP_FAST_POST_MS"] = "0"
        consider("CB0", cb0)
        if cb0["del"] < 20:
            report("CB0", cb0, "CB1 callback+10ms")
            cb1 = run_test(
                "CB1_10",
                {**CUR, "AE_EXP_FAST_TEST_ID": "12", "AE_EXP_FAST_POST_MODE": "2"},
                20,
            )
            if pass20(cb1):
                CUR["AE_EXP_FAST_POST_MODE"] = "2"
                report("CB1_10", cb1, "PRE sweep")
            else:
                report("CB1_10", cb1, "CB2 callback+25ms")
                cb2 = run_test(
                    "CB2_25",
                    {**CUR, "AE_EXP_FAST_TEST_ID": "13", "AE_EXP_FAST_POST_MODE": "3"},
                    20,
                )
                if pass20(cb2):
                    CUR["AE_EXP_FAST_POST_MODE"] = "3"
                    report("CB2_25", cb2, "PRE sweep")
                else:
                    CUR["AE_EXP_FAST_POST_MODE"] = "0"
                    CUR["AE_EXP_FAST_POST_MS"] = "300"
                    report("CB2_25", cb2, "callback weak — PRE with POST=300")
        else:
            report("CB0", cb0, "PRE sweep (callback POST)")

    # 8. PRE sweep
    best_pre = int(CUR["AE_EXP_FAST_PRE_MS"])
    pre_vals = [200, 150, 100, 75, 50, 25, 0]
    for i, pre in enumerate(pre_vals):
        name = f"PRE_{pre}"
        rr = run_test(
            name,
            {**CUR, "AE_EXP_FAST_TEST_ID": str(20 + i), "AE_EXP_FAST_PRE_MS": str(pre)},
            20,
        )
        nxt = f"PRE_{pre_vals[i+1]}" if i + 1 < len(pre_vals) else "POST sweep or 2D"
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
            )
            if rr2["del"] >= 19:
                report(name + "_R", rr2, nxt)
                best_pre = pre
                continue
        report(name, rr, f"PRE stop at {pre}; use best_pre={best_pre}")
        log(f"PRE {pre} too aggressive — stop")
        break
    CUR["AE_EXP_FAST_PRE_MS"] = str(best_pre)

    # 9. POST sweep if fixed delay
    best_post = int(CUR.get("AE_EXP_FAST_POST_MS", "300"))
    if CUR.get("AE_EXP_FAST_POST_MODE", "0") == "0":
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
            )
            nxt = (
                f"POST_{post_vals[i+1]}"
                if i + 1 < len(post_vals)
                else "2D neighbors"
            )
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
                )
                if rr2["del"] >= 19:
                    report(name + "_R", rr2, nxt)
                    best_post = post
                    continue
            report(name, rr, f"POST stop; best_post={best_post}")
            break
        CUR["AE_EXP_FAST_POST_MS"] = str(best_post)

    # 10. 2D neighbors
    p = int(CUR["AE_EXP_FAST_PRE_MS"])
    q = int(CUR.get("AE_EXP_FAST_POST_MS", "0"))
    neighbors = []
    if CUR.get("AE_EXP_FAST_POST_MODE", "0") == "0":
        neighbors = [
            (max(0, p - 25), q + 25),
            (p + 25, max(0, q - 25)),
            (max(0, p - 50), q + 50),
            (p + 50, max(0, q - 50)),
        ]
    for i, (pp, qq) in enumerate(neighbors):
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
        )
        report(name, rr, "next 2D or VAL100")
        if pass20(rr) and BEST and BEST.get("name") == name:
            CUR["AE_EXP_FAST_PRE_MS"] = str(pp)
            CUR["AE_EXP_FAST_POST_MS"] = str(qq)

    # Align CUR to BEST delays if best is a PRE/POST/2D variant
    if BEST:
        if "pre" in BEST:
            CUR["AE_EXP_FAST_PRE_MS"] = str(BEST["pre"])
        if "post" in BEST and CUR.get("AE_EXP_FAST_POST_MODE", "0") == "0":
            CUR["AE_EXP_FAST_POST_MS"] = str(BEST["post"])
        if "pm" in BEST:
            CUR["AE_EXP_FAST_POST_MODE"] = str(BEST["pm"])

    # 11. Validation
    log(f"validation flags={CUR}")
    v100 = run_test(
        "VAL100", {**CUR, "AE_EXP_FAST_TEST_ID": "80"}, 100, timeout_s=45 * 100 + 300
    )
    report("VAL100", v100, "VAL200 or VAL100_SAFE")
    if v100["del"] < 98:
        cur2 = dict(CUR)
        cur2["AE_EXP_FAST_PRE_MS"] = "200"
        if cur2.get("AE_EXP_FAST_POST_MODE", "0") == "0":
            cur2["AE_EXP_FAST_POST_MS"] = "300"
        v100b = run_test(
            "VAL100_SAFE",
            {**cur2, "AE_EXP_FAST_TEST_ID": "81"},
            100,
            timeout_s=45 * 100 + 300,
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
            timeout_s=45 * 100 + 300,
        )
        report("VAL100_REPEAT", v100r, "VAL200")

    v200 = run_test(
        "VAL200", {**CUR, "AE_EXP_FAST_TEST_ID": "90"}, 200, timeout_s=45 * 200 + 300
    )
    report("VAL200", v200, "D1 AMPDU TX off")

    # 12. AMPDU
    d1 = run_test(
        "D1_AMPDU_TX_OFF",
        {**CUR, "AE_EXP_FAST_TEST_ID": "91", "AE_EXP_FAST_AMPDU_TX_OFF": "1"},
        20,
    )
    report("D1_AMPDU_TX_OFF", d1, "STORAGE_RAM")

    # 13. storage RAM
    ram = run_test(
        "STORAGE_RAM",
        {**CUR, "AE_EXP_FAST_TEST_ID": "92", "AE_EXP_FAST_STORAGE_RAM": "1"},
        20,
    )
    report("STORAGE_RAM", ram, "write report + restore WPA3")

    log("=== SCREENING COMPLETE ===")
    log(f"BEST {BEST}")
    log(f"VAL200 {v200}")
    STATE.write_text(
        json.dumps({"best": BEST, "cur": CUR, "val200": v200}, indent=2),
        encoding="utf-8",
    )

    # Restore WPA3 SAE for build tree (do not leave production-weakened)
    restore_wpa3()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        log(f"FATAL {ex}")
        raise
