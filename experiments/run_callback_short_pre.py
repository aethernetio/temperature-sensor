"""Short PRE search for late TX-done callback path (WPA2). N=50 screens + VAL200."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_fastest_path import (  # noqa: E402
    BUILD,
    ROOT,
    RX_LOG,
    cmake_configure,
    ensure_receiver,
    flash,
    log,
    ninja_build,
)

RESULTS = ROOT / "experiments" / "prepared_wifi_callback_wpa2.tsv"
CHAT = ROOT / "experiments" / "callback_short_pre_chat.txt"
STATE = ROOT / "experiments" / "callback_short_pre_state.json"
PROGRESS = ROOT / "experiments" / "callback_short_pre_progress.log"

RESULT_RE_EXT = re.compile(
    r"TEST_RESULT test_id=(?P<id>\d+) n=(?P<n>\d+) delivered=(?P<del>\d+)/(?P<plan>\d+) "
    r"connect_med_ms=(?P<conn>\d+) cycle_med_ms=(?P<cyc>\d+) p90_ms=(?P<p90>\d+) "
    r"max_ms=(?P<mx>\d+) wifi_ready=(?P<wr>\d+) encode=(?P<enc>\d+) sendto=(?P<st>\d+) "
    r"nonce=(?P<nonce>\d+) pre=(?P<pre>\d+) post=(?P<post>\d+) assoc=0x(?P<assoc>[0-9a-fA-F]+) "
    r"auth=(?P<auth>\d+) retry=(?P<retry>\d+) post_mode=(?P<pm>\d+) cb_any=(?P<cba>\d+) "
    r"cb_match=(?P<cbm>\d+)(?: cb_timeout=(?P<cbt>\d+))?(?: txdone_med_ms=(?P<txd>\d+))?"
    r"(?: teardown_med_ms=(?P<td>\d+))?(?: missing=(?P<miss>\d+))?"
    r"(?: duplicates=(?P<dup>\d+))?(?: ooo=(?P<ooo>\d+))? samples=(?P<samp>\d+)"
)

BASE = {
    "AE_EXP_FAST_USE_BSSID": "0",
    "AE_EXP_FAST_FAST_SCAN": "0",
    "AE_EXP_FAST_AUTH": "2",
    "AE_EXP_FAST_RETRY": "10",
    "AE_EXP_FAST_POST_MODE": "1",
    "AE_EXP_FAST_POST_MS": "0",
    "AE_EXP_FAST_AMPDU_TX_OFF": "0",
    "AE_EXP_FAST_STORAGE_RAM": "0",
    "AE_EXP_FAST_DISABLE_WPA3": "1",
}

OLD_PRE200_CYCLE = 430  # confirmed VAL200 callback PRE=200


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


def judge(r: dict) -> str:
    plan = r.get("plan", 0)
    if r.get("auth", 0) != 3:
        return "INVALID_AUTH"
    if r.get("wr", 0) < plan or r.get("enc", 0) < plan or r.get("st", 0) < plan:
        return "LIFECYCLE_FAIL"
    if r.get("cba", 0) < plan - 1:  # allow 1 miss on cb accounting
        return "CALLBACK_WEAK"
    if r.get("cbt", 0) > max(1, plan // 25):
        return "TIMEOUT_HIGH"
    # UDP losses OK; catastrophic = <70%
    if r["del"] < int(plan * 0.70):
        return "FAIL"
    return "PASS"


def write_chat(
    test_no: int, remaining: int, name: str, r: dict, best: dict | None, nxt: str
) -> None:
    result = judge(r)
    best_s = "none"
    if best:
        best_s = (
            f"PRE={best.get('pre')} cycle={best.get('cyc')}ms "
            f"del={best.get('del')}/{best.get('plan')} cb={best.get('cba')}"
        )
    lines = [
        f"[TEST {test_no}/{test_no + remaining}] {name}",
        f"delivery={r['del']}/{r['plan']}",
        f"callback={r.get('cba', 0)}/{r.get('wr', r['plan'])}",
        f"timeouts={r.get('cbt', 0)}",
        f"missing={r.get('miss', r['plan'] - r['del'])} dup={r.get('dup', 0)} ooo={r.get('ooo', 0)}",
        f"connect_med={r['conn']}",
        f"txdone_med={r.get('txd', 0)}",
        f"teardown_med={r.get('td', 0)}",
        f"cycle_med={r['cyc']}",
        f"p90={r['p90']}",
        f"max={r['mx']}",
        f"result={result}",
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
        "auth\tcb_seen\tcb_timeout\tmissing\tduplicates\tooo\tsamples\n"
    )
    need_header = not RESULTS.exists()
    if RESULTS.exists():
        first = RESULTS.read_text(encoding="utf-8").splitlines()[:1]
        if first and "missing" not in first[0]:
            # keep old file; append with extended header note via compatible columns
            pass
    if need_header:
        RESULTS.write_text(header, encoding="utf-8")
    line = (
        f"{name}\t{r['id']}\t{r['n']}\t{r['del']}\t{r['plan']}\t{r['conn']}\t"
        f"{r.get('txd',0)}\t{r.get('td',0)}\t{r['cyc']}\t{r['p90']}\t{r['mx']}\t"
        f"{r['wr']}\t{r['enc']}\t{r['st']}\t{r['nonce']}\t{r['pre']}\t{r['pm']}\t"
        f"{r['auth']}\t{r.get('cba',0)}\t{r.get('cbt',0)}\t"
        f"{r.get('miss', r['plan']-r['del'])}\t{r.get('dup',0)}\t{r.get('ooo',0)}\t"
        f"{r['samp']}\n"
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
    force_wpa3_off()
    prev = count_results()
    flash()
    r = wait_result_ext(prev, timeout_s, expect_id=test_id, expect_n=n)
    r["pre"] = pre_ms
    if "miss" not in r or r["miss"] == 0 and r["del"] < r["plan"]:
        r["miss"] = r["plan"] - r["del"]
    append_tsv(name, r)
    return r


def consider_best(best: dict | None, name: str, r: dict) -> dict:
    if judge(r) not in ("PASS",):
        return best if best else {
            "name": name,
            "pre": r["pre"],
            "cyc": r["cyc"],
            "del": r["del"],
            "plan": r["plan"],
            "cba": r.get("cba", 0),
            "cbt": r.get("cbt", 0),
            "conn": r["conn"],
            "txd": r.get("txd", 0),
            "p90": r["p90"],
            "mx": r["mx"],
        }
    cand = {
        "name": name,
        "pre": r["pre"],
        "cyc": r["cyc"],
        "del": r["del"],
        "plan": r["plan"],
        "cba": r.get("cba", 0),
        "cbt": r.get("cbt", 0),
        "conn": r["conn"],
        "txd": r.get("txd", 0),
        "p90": r["p90"],
        "mx": r["mx"],
    }
    if best is None:
        return cand
    # Prefer shorter PRE if cycle not worse by much and delivery OK; else faster cycle
    if cand["pre"] < best["pre"] and cand["cyc"] <= best["cyc"] + 20:
        return cand
    if cand["cyc"] < best["cyc"]:
        return cand
    return best


def screen_ok_for_val200(r: dict) -> bool:
    return (
        judge(r) == "PASS"
        and r.get("auth", 0) == 3
        and r.get("cba", 0) >= r["plan"] - 0
        and r.get("cbt", 0) <= 1
        and r["wr"] == r["plan"]
        and r["enc"] == r["plan"]
        and r["st"] == r["plan"]
    )


def main() -> int:
    CHAT.write_text("", encoding="utf-8")
    # Ensure TSV has extended header for new rows; keep prior history via append
    if not RESULTS.exists():
        RESULTS.write_text(
            "name\ttest_id\tn\tdelivered\tplan\tconnect_med\ttxdone_med\tteardown_med\t"
            "cycle_med\tp90\tmax\twifi_ready\tencode\tsendto\tnonce\tpre\tpost_mode\t"
            "auth\tcb_seen\tcb_timeout\tmissing\tduplicates\tooo\tsamples\n",
            encoding="utf-8",
        )
    else:
        # Append a blank separator comment isn't valid TSV; just continue appending
        pass

    best: dict | None = None
    results: dict = {}
    test_no = 1
    remaining = 6
    val200_by_pre: dict[int, dict] = {}

    # --- PRE25_N50 ---
    name = "PRE25_N50"
    log(f"=== {name} ===")
    r25 = run_one(name, test_id=201, n=50, pre_ms=25, timeout_s=1800)
    results[name] = r25
    best = consider_best(best, name, r25)
    write_chat(test_no, remaining - 1, name, r25, best, "PRE25_VAL200 if OK")
    test_no += 1
    remaining -= 1

    if screen_ok_for_val200(r25):
        name = "PRE25_VAL200"
        log(f"=== {name} ===")
        r = run_one(name, test_id=202, n=200, pre_ms=25, timeout_s=7200)
        results[name] = r
        val200_by_pre[25] = r
        best = consider_best(best, name, r)
        write_chat(test_no, remaining - 1, name, r, best, "PRE10_N50")
        test_no += 1
        remaining -= 1
    else:
        log("PRE25 screen not OK for VAL200 — skip VAL200")

    # --- PRE10_N50 ---
    name = "PRE10_N50"
    log(f"=== {name} ===")
    r10 = run_one(name, test_id=203, n=50, pre_ms=10, timeout_s=1800)
    results[name] = r10
    best = consider_best(best, name, r10)
    write_chat(test_no, remaining - 1, name, r10, best, "PRE0_N50")
    test_no += 1
    remaining -= 1

    # --- PRE0_N50 ---
    name = "PRE0_N50"
    log(f"=== {name} ===")
    r0 = run_one(name, test_id=204, n=50, pre_ms=0, timeout_s=1800)
    results[name] = r0
    best = consider_best(best, name, r0)
    write_chat(test_no, remaining - 1, name, r0, best, "VAL200 for best short PRE")
    test_no += 1
    remaining -= 1

    # Pick best short PRE among 0/10/25 that passed screen
    candidates = []
    for pre, rr in ((0, r0), (10, r10), (25, r25)):
        if screen_ok_for_val200(rr):
            candidates.append((pre, rr))
    # Prefer shortest PRE among passing screens
    candidates.sort(key=lambda x: (x[0], x[1]["cyc"]))

    winner_pre = 25
    if candidates:
        winner_pre = candidates[0][0]
    else:
        # fallback: least-bad by delivery then cycle
        ranked = sorted(
            [(0, r0), (10, r10), (25, r25)],
            key=lambda x: (-x[1]["del"], x[1]["cyc"]),
        )
        winner_pre = ranked[0][0]

    # VAL200 for winner if not already done (PRE25 may already have VAL200)
    if winner_pre not in val200_by_pre and screen_ok_for_val200(
        {0: r0, 10: r10, 25: r25}[winner_pre]
    ):
        name = f"PRE{winner_pre}_VAL200"
        log(f"=== {name} ===")
        r = run_one(
            name, test_id=210 + winner_pre, n=200, pre_ms=winner_pre, timeout_s=7200
        )
        results[name] = r
        val200_by_pre[winner_pre] = r
        best = consider_best(best, name, r)
        write_chat(test_no, remaining - 1, name, r, best, "report")
        test_no += 1
        remaining -= 1
    elif winner_pre in val200_by_pre:
        log(f"VAL200 already done for PRE={winner_pre}")
        best = consider_best(best, f"PRE{winner_pre}_VAL200", val200_by_pre[winner_pre])
    else:
        log(f"No VAL200 for winner PRE={winner_pre} (screen not OK)")

    # If PRE0 or PRE10 passed and is shorter than PRE25 and better, ensure VAL200
    # (already handled by winner_pre)

    STATE.write_text(
        json.dumps(
            {
                "results": results,
                "val200": {str(k): v for k, v in val200_by_pre.items()},
                "winner_pre": winner_pre,
                "best": best,
                "old_pre200_cycle": OLD_PRE200_CYCLE,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    log(f"DONE winner_pre={winner_pre} best={best}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
