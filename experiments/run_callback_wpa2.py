"""Late TX-done callback prepared-send campaign (WPA2, PRE=200 then PRE sweep)."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_fastest_path import (  # noqa: E402
    BUILD,
    RESULT_RE,
    ROOT,
    RX_LOG,
    cmake_configure,
    ensure_receiver,
    env,
    flash,
    log,
    ninja_build,
    wait_result,
)

RESULTS = ROOT / "experiments" / "prepared_wifi_callback_wpa2.tsv"
CHAT = ROOT / "experiments" / "callback_wpa2_chat.txt"
STATE = ROOT / "experiments" / "callback_wpa2_state.json"
PROGRESS = ROOT / "experiments" / "callback_wpa2_progress.log"

# Extended regex with new fields (falls back handled by groupdict defaults).
RESULT_RE_EXT = re.compile(
    r"TEST_RESULT test_id=(?P<id>\d+) n=(?P<n>\d+) delivered=(?P<del>\d+)/(?P<plan>\d+) "
    r"connect_med_ms=(?P<conn>\d+) cycle_med_ms=(?P<cyc>\d+) p90_ms=(?P<p90>\d+) "
    r"max_ms=(?P<mx>\d+) wifi_ready=(?P<wr>\d+) encode=(?P<enc>\d+) sendto=(?P<st>\d+) "
    r"nonce=(?P<nonce>\d+) pre=(?P<pre>\d+) post=(?P<post>\d+) assoc=0x(?P<assoc>[0-9a-fA-F]+) "
    r"auth=(?P<auth>\d+) retry=(?P<retry>\d+) post_mode=(?P<pm>\d+) cb_any=(?P<cba>\d+) "
    r"cb_match=(?P<cbm>\d+)(?: cb_timeout=(?P<cbt>\d+))?(?: txdone_med_ms=(?P<txd>\d+))?"
    r"(?: teardown_med_ms=(?P<td>\d+))? samples=(?P<samp>\d+)"
)

BASE = {
    "AE_EXP_FAST_USE_BSSID": "0",
    "AE_EXP_FAST_FAST_SCAN": "0",
    "AE_EXP_FAST_AUTH": "2",
    "AE_EXP_FAST_RETRY": "10",
    "AE_EXP_FAST_POST_MODE": "1",  # kTxDoneCb — late install before sendto
    "AE_EXP_FAST_POST_MS": "0",
    "AE_EXP_FAST_AMPDU_TX_OFF": "0",
    "AE_EXP_FAST_STORAGE_RAM": "0",
    "AE_EXP_FAST_DISABLE_WPA3": "1",
}

OLD_WINNER_CYCLE = 710


def force_wpa3_off() -> None:
    sdk = BUILD / "sdkconfig"
    if not sdk.exists():
        return
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


def count_results() -> int:
    if not RX_LOG.exists():
        return 0
    text = RX_LOG.read_text(encoding="utf-8", errors="replace")
    return len(list(RESULT_RE_EXT.finditer(text)))


def wait_result_ext(
    prev: int, timeout_s: int, expect_id: int | None, expect_n: int | None
) -> dict:
    deadline = time.time() + timeout_s
    last_hb = 0.0
    while time.time() < deadline:
        now = time.time()
        if now - last_hb >= 30:
            last_hb = now
            log(f"waiting TEST_RESULT prev={prev} left={int(deadline-now)}s")
        if RX_LOG.exists():
            text = RX_LOG.read_text(encoding="utf-8", errors="replace")
            matches = list(RESULT_RE_EXT.finditer(text))
            if len(matches) > prev:
                for m in reversed(matches[prev:]):
                    gd = m.groupdict()
                    d = {}
                    for k, v in gd.items():
                        if v is None:
                            d[k] = 0
                        elif k == "assoc":
                            d[k] = int(v, 16)
                        else:
                            d[k] = int(v)
                    if expect_id is not None and d["id"] != expect_id:
                        continue
                    if expect_n is not None and d["n"] != expect_n:
                        continue
                    return d
        time.sleep(2)
    raise TimeoutError("no TEST_RESULT")


def write_chat(test_no: int, remaining: int, name: str, r: dict, best: dict | None, nxt: str) -> None:
    auth_ok = r.get("auth", 0) == 3
    cb = r.get("cba", 0)
    to = r.get("cbt", 0)
    plan = r.get("plan", 0)
    result = "PASS"
    if not auth_ok:
        result = "INVALID_AUTH"
    elif plan >= 20 and cb < max(18, int(plan * 0.9)):
        result = "CALLBACK_WEAK"
    elif r["del"] < int(plan * 0.85):
        result = "FAIL"
    best_s = "none"
    if best:
        best_s = (
            f"PRE={best.get('pre')} POST=callback cycle={best.get('cyc')}ms "
            f"del={best.get('del')}/{best.get('plan')} cb={best.get('cba')}"
        )
    lines = [
        f"[TEST {test_no}/{test_no + remaining}] CALLBACK_WPA2_{name}",
        f"delivery={r['del']}/{r['plan']}",
        f"callback={cb}/{r.get('wr', plan)}",
        f"timeouts={to}",
        f"connect_med={r['conn']}",
        f"txdone_med={r.get('txd', 0)}",
        f"cycle_med={r['cyc']}",
        f"p90={r['p90']}",
        f"auth={r.get('auth')} result={result}",
        f"remaining={remaining}",
        "BEST NOW:",
        best_s,
        f"NEXT: {nxt}",
        "",
    ]
    text = "\n".join(lines)
    CHAT.parent.mkdir(parents=True, exist_ok=True)
    with CHAT.open("a", encoding="utf-8") as f:
        f.write(text + "\n")
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text, flush=True)


def append_tsv(name: str, r: dict) -> None:
    header = (
        "name\ttest_id\tn\tdelivered\tplan\tconnect_med\ttxdone_med\tteardown_med\t"
        "cycle_med\tp90\tmax\twifi_ready\tencode\tsendto\tnonce\tpre\tpost_mode\t"
        "auth\tcb_seen\tcb_timeout\tsamples\n"
    )
    if not RESULTS.exists():
        RESULTS.write_text(header, encoding="utf-8")
    line = (
        f"{name}\t{r['id']}\t{r['n']}\t{r['del']}\t{r['plan']}\t{r['conn']}\t"
        f"{r.get('txd',0)}\t{r.get('td',0)}\t{r['cyc']}\t{r['p90']}\t{r['mx']}\t"
        f"{r['wr']}\t{r['enc']}\t{r['st']}\t{r['nonce']}\t{r['pre']}\t{r['pm']}\t"
        f"{r['auth']}\t{r.get('cba',0)}\t{r.get('cbt',0)}\t{r['samp']}\n"
    )
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(line)


def run_one(name: str, test_id: int, n: int, pre_ms: int, timeout_s: int) -> dict:
    flags = {
        **BASE,
        "AE_EXP_FAST_TEST_ID": str(test_id),
        "AE_EXP_FAST_N": str(n),
        "AE_EXP_FAST_PRE_MS": str(pre_ms),
    }
    ensure_receiver()
    cmake_configure(flags)
    force_wpa3_off()
    ninja_build()
    # Re-apply after ninja may regenerate sdkconfig from defaults
    force_wpa3_off()
    prev = count_results()
    flash()
    r = wait_result_ext(prev, timeout_s, expect_id=test_id, expect_n=n)
    r["pre"] = pre_ms
    append_tsv(name, r)
    return r


def main() -> int:
    RESULTS.write_text(
        "name\ttest_id\tn\tdelivered\tplan\tconnect_med\ttxdone_med\tteardown_med\t"
        "cycle_med\tp90\tmax\twifi_ready\tencode\tsendto\tnonce\tpre\tpost_mode\t"
        "auth\tcb_seen\tcb_timeout\tsamples\n",
        encoding="utf-8",
    )
    CHAT.write_text("", encoding="utf-8")
    best: dict | None = None
    test_no = 1
    remaining = 12

    # --- VAL20 screen ---
    name = "VAL20_PRE200"
    log(f"=== {name} ===")
    r = run_one(name, test_id=101, n=20, pre_ms=200, timeout_s=900)
    write_chat(test_no, remaining - 1, name, r, best, "VAL100 if cb>=18")
    test_no += 1
    remaining -= 1

    if r.get("auth", 0) != 3:
        log("INVALID: authmode != WPA2_PSK(3)")
        STATE.write_text(json.dumps({"stop": "invalid_auth", "r": r}, indent=2), encoding="utf-8")
        return 2
    if r["wr"] < 20 or r["enc"] < 20 or r["st"] < 20 or r["nonce"] < 20:
        log(f"SCREEN FAIL lifecycle wr={r['wr']} enc={r['enc']} st={r['st']} nonce={r['nonce']}")
        STATE.write_text(json.dumps({"stop": "lifecycle", "r": r}, indent=2), encoding="utf-8")
        return 3
    if r.get("cba", 0) < 18:
        log(f"SCREEN STOP callback_seen={r.get('cba')}/20")
        STATE.write_text(json.dumps({"stop": "callback_weak", "r": r}, indent=2), encoding="utf-8")
        return 4

    best = {
        "name": name,
        "pre": 200,
        "cyc": r["cyc"],
        "conn": r["conn"],
        "del": r["del"],
        "plan": r["plan"],
        "cba": r.get("cba", 0),
        "cbt": r.get("cbt", 0),
        "txd": r.get("txd", 0),
        "p90": r["p90"],
        "mx": r["mx"],
    }

    if r["del"] < 18:
        log("delivery <18/20 — continue only if callback strong; stopping per screen gate soft")
        # User: if delivery >=18 and callback almost always → continue
        STATE.write_text(json.dumps({"stop": "delivery_screen", "r": r}, indent=2), encoding="utf-8")
        return 5

    # --- VAL100 ---
    name = "VAL100_PRE200"
    log(f"=== {name} ===")
    r = run_one(name, test_id=102, n=100, pre_ms=200, timeout_s=3600)
    write_chat(test_no, remaining - 1, name, r, best, "VAL200 if normal")
    test_no += 1
    remaining -= 1
    if r.get("auth", 0) != 3:
        log("INVALID auth on VAL100")
        return 2
    best = {
        "name": name,
        "pre": 200,
        "cyc": r["cyc"],
        "conn": r["conn"],
        "del": r["del"],
        "plan": r["plan"],
        "cba": r.get("cba", 0),
        "cbt": r.get("cbt", 0),
        "txd": r.get("txd", 0),
        "p90": r["p90"],
        "mx": r["mx"],
    }
    STATE.write_text(json.dumps({"val100": r, "best": best}, indent=2), encoding="utf-8")

    # Gate: callback mostly works
    if r.get("cba", 0) < 90:
        log("VAL100 callback weak — stop before VAL200")
        return 6

    # --- VAL200 ---
    name = "VAL200_PRE200"
    log(f"=== {name} ===")
    r = run_one(name, test_id=103, n=200, pre_ms=200, timeout_s=7200)
    write_chat(test_no, remaining - 1, name, r, best, "PRE sweep if stable")
    test_no += 1
    remaining -= 1
    val200 = dict(r)
    best = {
        "name": name,
        "pre": 200,
        "cyc": r["cyc"],
        "conn": r["conn"],
        "del": r["del"],
        "plan": r["plan"],
        "cba": r.get("cba", 0),
        "cbt": r.get("cbt", 0),
        "txd": r.get("txd", 0),
        "p90": r["p90"],
        "mx": r["mx"],
    }
    saving = OLD_WINNER_CYCLE - r["cyc"]
    log(
        f"COMPARE old={OLD_WINNER_CYCLE} new={r['cyc']} saving={saving}ms "
        f"({100.0 * saving / OLD_WINNER_CYCLE:.1f}%)"
    )

    # --- PRE sweep (screen 20) if callback stable ---
    pre_sweep_results = []
    if r.get("cba", 0) >= 180 and r["cyc"] < OLD_WINNER_CYCLE:
        for pre in (150, 100, 75, 50, 25, 0):
            name = f"PRE{pre}_S20"
            log(f"=== {name} ===")
            rr = run_one(name, test_id=110 + pre // 5, n=20, pre_ms=pre, timeout_s=900)
            write_chat(test_no, max(0, remaining - 1), name, rr, best, f"next PRE")
            test_no += 1
            remaining = max(0, remaining - 1)
            pre_sweep_results.append((pre, rr))
            cb_ok = rr.get("cba", 0) >= 18
            del_ok = rr["del"] >= 15  # UDP losses ok; not sharply worse
            if not cb_ok:
                log(f"PRE={pre} callback weak — stop sweep")
                break
            if cb_ok and del_ok and rr["cyc"] <= best["cyc"]:
                best = {
                    "name": name,
                    "pre": pre,
                    "cyc": rr["cyc"],
                    "conn": rr["conn"],
                    "del": rr["del"],
                    "plan": rr["plan"],
                    "cba": rr.get("cba", 0),
                    "cbt": rr.get("cbt", 0),
                    "txd": rr.get("txd", 0),
                    "p90": rr["p90"],
                    "mx": rr["mx"],
                }
            # If delivery collapses sharply vs VAL20 baseline 18+, stop
            if rr["del"] < 12:
                log(f"PRE={pre} delivery collapsed — stop sweep")
                break

    STATE.write_text(
        json.dumps(
            {
                "val200": val200,
                "best": best,
                "pre_sweep": [(p, x) for p, x in pre_sweep_results],
                "old_winner": OLD_WINNER_CYCLE,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"DONE best={best}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
