"""Silent fastest-path prepared Wi-Fi campaign orchestrator (ESP32-C6)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
BUILD = ROOT / "build-esp32c6-save-bench-smoke"
AETHER = r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0"
PY = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
CMAKE = Path(r"C:\Espressif\tools\cmake\3.30.2\bin\cmake.exe")
NINJA = Path(r"C:\Espressif\tools\ninja\1.12.1\ninja.exe")
RX_EXE = ROOT / "temperature_receiver" / "build-bisect" / "temperature_receiver.exe"
RX_SESSION = ROOT / "experiments" / "prepared_wifi_cache_rx_session"
RX_LOG = ROOT / "experiments" / "prepared_wifi_fastest_rx.log"
RESULTS = ROOT / "experiments" / "prepared_wifi_fastest_path.tsv"
PROGRESS = ROOT / "experiments" / "fastest_progress.log"
STATE = ROOT / "experiments" / "fastest_state.json"
CHAT = ROOT / "experiments" / "fastest_chat.txt"

IDF_PATH = r"C:\Espressif\frameworks\esp-idf-v6.0.2"
CCACHE = r"C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64"

BASE_CMAKE = {
    "CPM_aether-client-cpp_SOURCE": AETHER,
    "AE_EXP_PREPARED_WIFI_FASTEST": "1",
    "AE_EXP_PREPARED_WIFI_BISECT": "",
    "AE_EXP_BISECT_CONSOLE": "",
    "AE_EXP_BISECT_SMOKE": "",
    "AE_EXP_SKIP_DTOR_SAVE": "1",
    "SERVICE_UID": "5aade50f-00d9-4624-b097-e203cdcf1e38",
    "BENCH_CLIENT_ID": "prepared_wifi_bisect_v1",
    "WIFI_SSID": "chirkov",
    "WIFI_PASSWORD": "kcdjepWz51",
    "AETHER_PREPARED_POST_SEND_HOLD_MS": "300",
    "AE_EXP_FAST_DISABLE_WPA3": "",
}

RESULT_RE = re.compile(
    r"TEST_RESULT test_id=(?P<id>\d+) n=(?P<n>\d+) delivered=(?P<del>\d+)/(?P<plan>\d+) "
    r"connect_med_ms=(?P<conn>\d+) cycle_med_ms=(?P<cyc>\d+) p90_ms=(?P<p90>\d+) "
    r"max_ms=(?P<mx>\d+) wifi_ready=(?P<wr>\d+) encode=(?P<enc>\d+) sendto=(?P<st>\d+) "
    r"nonce=(?P<nonce>\d+) pre=(?P<pre>\d+) post=(?P<post>\d+) assoc=0x(?P<assoc>[0-9a-fA-F]+) "
    r"auth=(?P<auth>\d+) retry=(?P<retry>\d+) post_mode=(?P<pm>\d+) cb_any=(?P<cba>\d+) "
    r"cb_match=(?P<cbm>\d+) samples=(?P<samp>\d+)"
)


def env() -> dict:
    e = os.environ.copy()
    e["IDF_PATH"] = IDF_PATH
    e["IDF_TOOLS_PATH"] = r"C:\Espressif"
    extra = [
        CCACHE,
        r"C:\Espressif\tools\ninja\1.12.1",
        r"C:\Espressif\tools\cmake\3.30.2\bin",
        r"C:\msys64\ucrt64\bin",
    ]
    e["Path"] = ";".join(extra) + ";" + e.get("Path", "")
    e.pop("CCACHE_DISABLE", None)
    return e


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_chat(test_no: int, remaining: int, name: str, r: dict, best: dict | None, nxt: str) -> None:
    result = "PASS" if r["del"] == r["plan"] and r["plan"] > 0 else "FAIL"
    if name.startswith("CB") and r.get("cbm", 0) < 15:
        result = "CALLBACK_NOT_USABLE"
    best_s = "none"
    if best:
        best_s = (
            f"{best.get('name','?')} cycle={best.get('cyc')}ms "
            f"connect={best.get('conn')}ms del={best.get('del')}/{best.get('plan')}"
        )
    delta = ""
    if best and best.get("name") != name:
        delta = f"change vs best={r['cyc'] - best['cyc']:+d} ms"
    elif best and best.get("name") == name:
        delta = "change vs best=NEW BEST"
    lines = [
        f"[TEST {test_no}/{test_no + remaining}] {name}",
        f"delivery={r['del']}/{r['plan']}",
        f"median_cycle={r['cyc']} ms",
        f"median_connect={r['conn']} ms",
        f"p90={r['p90']}",
        f"result={result}",
        f"remaining={remaining}",
        f"best={best_s}",
        delta,
        "BEST NOW:",
        f"config={best.get('name') if best else 'none'}",
        f"pre={best.get('pre') if best else '-'} post/callback={best.get('post') if best else '-'} mode={best.get('pm') if best else '-'}",
        f"median={best.get('cyc') if best else '-'}",
        f"NEXT: {nxt}",
        "",
    ]
    text = "\n".join(x for x in lines if x is not None)
    with CHAT.open("a", encoding="utf-8") as f:
        f.write(text + "\n")
    log("CHAT\n" + text)


def cmake_configure(flags: dict) -> None:
    args = [str(CMAKE), "-S", str(ROOT), "-B", str(BUILD), "-G", "Ninja"]
    merged = dict(BASE_CMAKE)
    merged.update(flags)
    for k, v in merged.items():
        args.append(f"-D{k}={v}")
    log("cmake " + " ".join(f"{k}={v}" for k, v in flags.items()))
    r = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "fastest_cmake.err").write_text(
            r.stdout + "\n" + r.stderr, encoding="utf-8"
        )
        raise RuntimeError("cmake failed")


def ninja_build() -> None:
    r = subprocess.run(
        [str(NINJA), "-C", str(BUILD)],
        env=env(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        (ROOT / "experiments" / "fastest_build.err").write_text(
            r.stdout[-8000:] + "\n" + r.stderr[-8000:], encoding="utf-8"
        )
        raise RuntimeError("ninja failed")
    log("build ok")


def flash() -> None:
    cmd = [
        str(PY),
        "-m",
        "esptool",
        "--chip",
        "esp32c6",
        "-p",
        "COM7",
        "-b",
        "460800",
        "write-flash",
        "--flash-size",
        "4MB",
        "0x0",
        str(BUILD / "bootloader" / "bootloader.bin"),
        "0x8000",
        str(BUILD / "partition_table" / "partition-table.bin"),
        "0x10000",
        str(BUILD / "temperature_sensor.bin"),
    ]
    r = subprocess.run(cmd, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "fastest_flash.err").write_text(
            r.stdout + "\n" + r.stderr, encoding="utf-8"
        )
        raise RuntimeError("flash failed")
    log("flash ok")


def ensure_receiver() -> None:
    # leave running if alive
    try:
        subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq temperature_receiver.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        pass
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq temperature_receiver.exe"],
        capture_output=True,
        text=True,
    ).stdout
    if "temperature_receiver.exe" in out:
        return
    RX_SESSION.mkdir(parents=True, exist_ok=True)
    env2 = env()
    env2["AE_RECEIVER_SESSION_DIR"] = str(RX_SESSION)
    RX_LOG.parent.mkdir(parents=True, exist_ok=True)
    # append mode via Start-Process equivalent
    with RX_LOG.open("a", encoding="utf-8") as outf, (
        ROOT / "experiments" / "prepared_wifi_fastest_rx.log.err"
    ).open("a", encoding="utf-8") as errf:
        subprocess.Popen(
            [str(RX_EXE)],
            cwd=str(RX_SESSION),
            env=env2,
            stdout=outf,
            stderr=errf,
        )
    time.sleep(4)
    log("receiver started")


def wait_result(
    prev_count: int,
    timeout_s: int,
    expect_id: int | None = None,
    expect_n: int | None = None,
) -> dict:
    deadline = time.time() + timeout_s
    last_hb = 0.0
    while time.time() < deadline:
        now = time.time()
        if now - last_hb >= 30:
            last_hb = now
            extra = ""
            if RX_LOG.exists():
                tail = RX_LOG.read_text(encoding="utf-8", errors="replace")[-400:]
                extra = " rx_tail=" + tail.replace("\n", " | ")[-200:]
            log(f"waiting TEST_RESULT prev={prev_count} left={int(deadline-now)}s{extra}")
        if RX_LOG.exists():
            text = RX_LOG.read_text(encoding="utf-8", errors="replace")
            matches = list(RESULT_RE.finditer(text))
            if len(matches) > prev_count:
                # Prefer the newest match that satisfies expect_* filters.
                for m in reversed(matches[prev_count:]):
                    d = {
                        k: int(v, 16) if k == "assoc" else int(v)
                        for k, v in m.groupdict().items()
                    }
                    if expect_id is not None and d["id"] != expect_id:
                        continue
                    if expect_n is not None and d["n"] != expect_n:
                        continue
                    return d
                # New lines exist but none match filters yet — keep waiting.
        time.sleep(2)
    raise TimeoutError("no TEST_RESULT")


def append_tsv(name: str, r: dict) -> None:
    new = not RESULTS.exists()
    with RESULTS.open("a", encoding="utf-8") as f:
        if new:
            f.write(
                "Variant\tAssociation\tAuth\tPRE\tPOST\tDelivered\tN\t"
                "ConnectMed\tCycleMed\tp90\tmax\tWifiReady\tEncode\tSendto\t"
                "CbAny\tCbMatch\tPostMode\n"
            )
        assoc = f"0x{r['assoc']:02x}"
        f.write(
            f"{name}\t{assoc}\t{r['auth']}\t{r['pre']}\t{r['post']}\t"
            f"{r['del']}/{r['plan']}\t{r['n']}\t{r['conn']}\t{r['cyc']}\t"
            f"{r['p90']}\t{r['mx']}\t{r['wr']}\t{r['enc']}\t{r['st']}\t"
            f"{r['cba']}\t{r['cbm']}\t{r['pm']}\n"
        )


def count_results() -> int:
    if not RX_LOG.exists():
        return 0
    return len(RESULT_RE.findall(RX_LOG.read_text(encoding="utf-8", errors="replace")))


def run_test(
    name: str,
    flags: dict,
    n: int = 20,
    timeout_s: int | None = None,
    rebuild: bool = True,
) -> dict:
    flags = dict(flags)
    flags.setdefault("AE_EXP_FAST_N", str(n))
    flags.setdefault("AETHER_PREPARED_NONCE_RESERVE", str(max(n, 20)))
    ensure_receiver()
    prev = count_results()
    if rebuild:
        cmake_configure(flags)
        ninja_build()
    flash()
    to = timeout_s if timeout_s is not None else max(180, 25 * n + 180)
    expect_id = None
    try:
        expect_id = int(flags.get("AE_EXP_FAST_TEST_ID") or 0) or None
    except Exception:
        expect_id = None
    r = wait_result(prev, to, expect_id=expect_id, expect_n=n)
    r["name"] = name
    append_tsv(name, r)
    log(
        f"[TEST] {name} delivery={r['del']}/{r['plan']} "
        f"median_cycle={r['cyc']} ms median_connect={r['conn']} ms "
        f"p90={r['p90']} wifi_ready={r['wr']}/{r['plan']}"
    )
    STATE.write_text(json.dumps({"last": name, "result": r}, indent=2), encoding="utf-8")
    return r


def pass20(r: dict) -> bool:
    return r["del"] == r["plan"] and r["plan"] > 0


def main() -> int:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    log("=== FASTEST PATH CAMPAIGN START ===")

    remaining = 17
    best = None

    def consider(name: str, r: dict) -> None:
        nonlocal best
        if not pass20(r) and r["n"] <= 20:
            return
        if best is None or r["cyc"] < best["cyc"] or (
            r["cyc"] == best["cyc"] and r["del"] > best["del"]
        ):
            if r["cyc"] + 20 < (best["cyc"] if best else 10**9) or best is None:
                best = dict(r)
                best["name"] = name
            elif best and abs(r["cyc"] - best["cyc"]) <= 20:
                # not a clear improvement
                if r["del"] > best["del"]:
                    best = dict(r)
                    best["name"] = name

    # 3. BASE
    base_flags = {
        "AE_EXP_FAST_TEST_ID": "1",
        "AE_EXP_FAST_PRE_MS": "200",
        "AE_EXP_FAST_POST_MS": "300",
        "AE_EXP_FAST_USE_BSSID": "0",
        "AE_EXP_FAST_FAST_SCAN": "0",
        "AE_EXP_FAST_AUTH": "0",
        "AE_EXP_FAST_RETRY": "10",
        "AE_EXP_FAST_POST_MODE": "0",
        "AE_EXP_FAST_AMPDU_TX_OFF": "0",
        "AE_EXP_FAST_STORAGE_RAM": "0",
    }
    r = run_test("BASE", base_flags, 20)
    if r["del"] < 20:
        log("BASE not 20/20 — repeating")
        r = run_test("BASE_REPEAT", base_flags, 20)
    if r["cyc"] > 1100 and r["del"] >= 18:
        log("WARN BASE median much worse than C6/C8 ~850ms")
    consider("BASE", r)
    remaining -= 1
    log(f"BEST NOW {best}")

    # 4. A1 A2 A3
    a_tests = [
        ("A1_BSSID", {**base_flags, "AE_EXP_FAST_TEST_ID": "2", "AE_EXP_FAST_USE_BSSID": "1"}),
        ("A2_FAST_SCAN", {**base_flags, "AE_EXP_FAST_TEST_ID": "3", "AE_EXP_FAST_FAST_SCAN": "1"}),
        ("A3_BSSID_FAST_SCAN", {
            **base_flags,
            "AE_EXP_FAST_TEST_ID": "4",
            "AE_EXP_FAST_USE_BSSID": "1",
            "AE_EXP_FAST_FAST_SCAN": "1",
        }),
    ]
    assoc_best_flags = dict(base_flags)
    assoc_best_name = "BASE"
    for name, flags in a_tests:
        rr = run_test(name, flags, 20)
        consider(name, rr)
        remaining -= 1
        if pass20(rr) and best and best.get("name") == name:
            assoc_best_flags = dict(flags)
            assoc_best_name = name
        log(f"BEST NOW {best}")

    # Keep BASE flags if A* not clearly faster
    if best and best.get("name") in ("BASE", "BASE_REPEAT"):
        assoc_best_flags = dict(base_flags)
        assoc_best_name = "BASE"

    # 5 AUTH
    auth_flags = dict(assoc_best_flags)
    r_auth1 = run_test("AUTH1_WPA3", {**auth_flags, "AE_EXP_FAST_TEST_ID": "5", "AE_EXP_FAST_AUTH": "0"}, 20)
    consider("AUTH1_WPA3", r_auth1)
    remaining -= 1
    r_auth2 = run_test("AUTH2_H2E", {**auth_flags, "AE_EXP_FAST_TEST_ID": "6", "AE_EXP_FAST_AUTH": "1"}, 20)
    consider("AUTH2_H2E", r_auth2)
    remaining -= 1
    r_auth3 = run_test("AUTH3_WPA2", {**auth_flags, "AE_EXP_FAST_TEST_ID": "7", "AE_EXP_FAST_AUTH": "2"}, 20)
    consider("AUTH3_WPA2", r_auth3)
    remaining -= 1

    auth_choice = 0
    auth_name = "AUTH1_WPA3"
    # pick fastest reliable among auth tests that passed; do not auto-weaken security
    for name, rr, aval in (
        ("AUTH1_WPA3", r_auth1, 0),
        ("AUTH2_H2E", r_auth2, 1),
        ("AUTH3_WPA2", r_auth3, 2),
    ):
        if pass20(rr) and (best is None or rr["cyc"] + 20 <= (best["cyc"] if best else 10**9)):
            if best and best.get("name") == name:
                auth_choice = aval
                auth_name = name
    if best and str(best.get("name", "")).startswith("AUTH"):
        if best["name"] == "AUTH2_H2E":
            auth_choice = 1
            auth_name = "AUTH2_H2E"
        elif best["name"] == "AUTH3_WPA2":
            auth_choice = 2
            auth_name = "AUTH3_WPA2"
        else:
            auth_choice = 0
            auth_name = "AUTH1_WPA3"

    cur = dict(assoc_best_flags)
    cur["AE_EXP_FAST_AUTH"] = str(auth_choice)
    log(f"auth selected {auth_name}={auth_choice} negotiated AUTH1={r_auth1['auth']} AUTH2={r_auth2['auth']} AUTH3={r_auth3['auth']}")

    # 6 retry
    r0 = run_test("R0", {**cur, "AE_EXP_FAST_TEST_ID": "8", "AE_EXP_FAST_RETRY": "0"}, 20)
    consider("R0", r0)
    remaining -= 1
    r1 = run_test("R1", {**cur, "AE_EXP_FAST_TEST_ID": "9", "AE_EXP_FAST_RETRY": "1"}, 20)
    consider("R1", r1)
    remaining -= 1
    r3 = run_test("R3", {**cur, "AE_EXP_FAST_TEST_ID": "10", "AE_EXP_FAST_RETRY": "3"}, 20)
    consider("R3", r3)
    remaining -= 1
    retry = 10
    # keep 10 unless a lower retry has same success-path median and no worse p90/delivery
    for name, rr, rv in (("R0", r0, 0), ("R1", r1, 1), ("R3", r3, 3)):
        if pass20(rr) and rr["wr"] >= 18:
            retry = rv  # last passing smaller? we'll set to first that matches median
            break
    # if success median same as AUTH, use smallest retry that didn't lose delivery
    if pass20(r0) and r0["wr"] >= 19:
        retry = 0
    elif pass20(r1) and r1["wr"] >= 19:
        retry = 1
    elif pass20(r3) and r3["wr"] >= 19:
        retry = 3
    cur["AE_EXP_FAST_RETRY"] = str(retry)
    log(f"retry selected {retry}")

    # 7 callback
    cb0 = run_test(
        "CB0",
        {**cur, "AE_EXP_FAST_TEST_ID": "11", "AE_EXP_FAST_POST_MODE": "1", "AE_EXP_FAST_POST_MS": "0"},
        20,
    )
    remaining -= 1
    cb_ok = pass20(cb0) and cb0["cbm"] >= 15
    if not cb_ok:
        log("CALLBACK_NOT_USABLE")
        cur["AE_EXP_FAST_POST_MODE"] = "0"
        cur["AE_EXP_FAST_POST_MS"] = "300"
    else:
        consider("CB0", cb0)
        cur["AE_EXP_FAST_POST_MODE"] = "1"
        cur["AE_EXP_FAST_POST_MS"] = "0"
        if cb0["del"] < 20:
            cb1 = run_test(
                "CB1_10",
                {**cur, "AE_EXP_FAST_TEST_ID": "12", "AE_EXP_FAST_POST_MODE": "2"},
                20,
            )
            remaining -= 1
            if pass20(cb1):
                consider("CB1_10", cb1)
                cur["AE_EXP_FAST_POST_MODE"] = "2"
            else:
                cb2 = run_test(
                    "CB2_25",
                    {**cur, "AE_EXP_FAST_TEST_ID": "13", "AE_EXP_FAST_POST_MODE": "3"},
                    20,
                )
                remaining -= 1
                if pass20(cb2):
                    consider("CB2_25", cb2)
                    cur["AE_EXP_FAST_POST_MODE"] = "3"
                else:
                    log("callback delivery weak — revert to 300ms hold")
                    cur["AE_EXP_FAST_POST_MODE"] = "0"
                    cur["AE_EXP_FAST_POST_MS"] = "300"
                    cb_ok = False

    # 8 PRE sweep
    pre_vals = [200, 150, 100, 75, 50, 25, 0]
    best_pre = 200
    for i, pre in enumerate(pre_vals):
        name = f"PRE_{pre}"
        rr = run_test(
            name,
            {**cur, "AE_EXP_FAST_TEST_ID": str(20 + i), "AE_EXP_FAST_PRE_MS": str(pre)},
            20,
        )
        remaining -= 1
        if rr["del"] >= 20:
            consider(name, rr)
            best_pre = pre
            continue
        if rr["del"] == 19:
            rr2 = run_test(name + "_R", {**cur, "AE_EXP_FAST_TEST_ID": str(40 + i), "AE_EXP_FAST_PRE_MS": str(pre)}, 20)
            remaining -= 1
            if rr2["del"] >= 19:
                consider(name + "_R", rr2)
                best_pre = pre
                continue
        log(f"PRE {pre} too aggressive ({rr['del']}/20) — stop PRE sweep")
        break
    cur["AE_EXP_FAST_PRE_MS"] = str(best_pre)

    # 9 POST sweep if not callback
    best_post = int(cur.get("AE_EXP_FAST_POST_MS", "300"))
    if cur.get("AE_EXP_FAST_POST_MODE", "0") == "0":
        post_vals = [300, 250, 200, 150, 100, 75, 50, 25, 0]
        for i, post in enumerate(post_vals):
            name = f"POST_{post}"
            rr = run_test(
                name,
                {**cur, "AE_EXP_FAST_TEST_ID": str(50 + i), "AE_EXP_FAST_POST_MS": str(post)},
                20,
            )
            remaining -= 1
            if rr["del"] >= 20:
                consider(name, rr)
                best_post = post
                continue
            if rr["del"] == 19:
                rr2 = run_test(
                    name + "_R",
                    {**cur, "AE_EXP_FAST_TEST_ID": str(60 + i), "AE_EXP_FAST_POST_MS": str(post)},
                    20,
                )
                remaining -= 1
                if rr2["del"] >= 19:
                    consider(name + "_R", rr2)
                    best_post = post
                    continue
            log(f"POST {post} too aggressive — stop POST sweep")
            break
        cur["AE_EXP_FAST_POST_MS"] = str(best_post)

    # 10 2D neighbors
    p = int(cur["AE_EXP_FAST_PRE_MS"])
    q = int(cur.get("AE_EXP_FAST_POST_MS", "0"))
    neighbors = [
        (max(0, p - 25), q + 25),
        (p + 25, max(0, q - 25)),
        (max(0, p - 50), q + 50),
        (p + 50, max(0, q - 50)),
    ]
    if cur.get("AE_EXP_FAST_POST_MODE", "0") != "0":
        neighbors = []  # callback path: don't 2D post delay
    for i, (pp, qq) in enumerate(neighbors):
        name = f"2D_p{pp}_q{qq}"
        rr = run_test(
            name,
            {
                **cur,
                "AE_EXP_FAST_TEST_ID": str(70 + i),
                "AE_EXP_FAST_PRE_MS": str(pp),
                "AE_EXP_FAST_POST_MS": str(qq),
            },
            20,
        )
        remaining -= 1
        if pass20(rr):
            consider(name, rr)
            if best and best.get("name") == name:
                cur["AE_EXP_FAST_PRE_MS"] = str(pp)
                cur["AE_EXP_FAST_POST_MS"] = str(qq)

    # 11 reliability 100 then 200
    cand_name = best["name"] if best else "BASE"
    log(f"validation candidate {cand_name} flags={cur}")
    v100 = run_test("VAL100", {**cur, "AE_EXP_FAST_TEST_ID": "80"}, 100, timeout_s=45 * 100)
    remaining -= 1
    if v100["del"] < 98:
        log("VAL100 < 98 — try BASE delays as fallback candidate")
        cur2 = dict(cur)
        cur2["AE_EXP_FAST_PRE_MS"] = "200"
        if cur2.get("AE_EXP_FAST_POST_MODE", "0") == "0":
            cur2["AE_EXP_FAST_POST_MS"] = "300"
        v100b = run_test("VAL100_SAFE", {**cur2, "AE_EXP_FAST_TEST_ID": "81"}, 100, timeout_s=45 * 100)
        if v100b["del"] > v100["del"]:
            cur = cur2
            v100 = v100b
    if v100["del"] == 98:
        v100r = run_test("VAL100_REPEAT", {**cur, "AE_EXP_FAST_TEST_ID": "82"}, 100, timeout_s=45 * 100)
        remaining -= 1

    v200 = run_test("VAL200", {**cur, "AE_EXP_FAST_TEST_ID": "90"}, 200, timeout_s=45 * 200)
    remaining -= 1

    # 12 AMPDU: default already TX off; explicit D1
    d1 = run_test(
        "D1_AMPDU_TX_OFF",
        {**cur, "AE_EXP_FAST_TEST_ID": "91", "AE_EXP_FAST_AMPDU_TX_OFF": "1"},
        20,
    )
    remaining -= 1
    consider("D1_AMPDU_TX_OFF", d1)

    # 13 storage RAM
    ram = run_test(
        "STORAGE_RAM",
        {**cur, "AE_EXP_FAST_TEST_ID": "92", "AE_EXP_FAST_STORAGE_RAM": "1"},
        20,
    )
    remaining -= 1
    consider("STORAGE_RAM", ram)

    log("=== SCREENING COMPLETE ===")
    log(f"BEST {best}")
    log(f"VAL200 {v200}")
    STATE.write_text(
        json.dumps({"best": best, "cur": cur, "val200": v200}, indent=2),
        encoding="utf-8",
    )
    return 0


def one_base_no_rebuild() -> dict:
    flags = {
        "AE_EXP_FAST_TEST_ID": "1",
        "AE_EXP_FAST_PRE_MS": "200",
        "AE_EXP_FAST_POST_MS": "300",
        "AE_EXP_FAST_USE_BSSID": "0",
        "AE_EXP_FAST_FAST_SCAN": "0",
        "AE_EXP_FAST_AUTH": "0",
        "AE_EXP_FAST_RETRY": "10",
        "AE_EXP_FAST_POST_MODE": "0",
        "AE_EXP_FAST_AMPDU_TX_OFF": "0",
        "AE_EXP_FAST_STORAGE_RAM": "0",
    }
    r = run_test("BASE", flags, 20, timeout_s=900, rebuild=False)
    write_chat(1, 21, "BASE", r, r, "A1 BASE+cached BSSID x20")
    return r


if __name__ == "__main__":
    try:
        if "--one-base" in sys.argv:
            one_base_no_rebuild()
            sys.exit(0)
        sys.exit(main())
    except Exception as ex:
        log(f"FATAL {ex}")
        raise
