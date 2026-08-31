#!/usr/bin/env python3
"""Resume chirkov Phase C from saved baseline + partial POST/sleep results."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "experiments" / "adaptive_probe_chirkov_c.lock"
spec = importlib.util.spec_from_file_location(
    "camp", ROOT / "experiments" / "run_adaptive_wifi_probe_campaign.py"
)
camp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(camp)

OUT = camp.OUT
AP = "chirkov"


def run_post(ap: str, wp: int, pre: int, post: int, results: dict) -> dict:
    camp.cmake_configure(
        ap,
        "C",
        {
            "AE_PROBE_OUTER": "1",
            "AE_PROBE_HOT_PER_OUTER": "30",
            "AE_PROBE_PROFILE": str(wp),
            "AE_PROBE_PRE_MS": str(pre),
            "AE_PROBE_POST_MS": str(post),
            "AE_PROBE_SLEEP_US": "1000000",
            "AE_PROBE_RUN_ID": str(10 + post),
            "BENCH_CLIENT_ID": "reliability_full_v1",
            "AETHER_PREPARED_NONCE_RESERVE": "30",
        },
    )
    camp.ninja_build()
    session = OUT / f"{ap}_rx_session"
    tsv_p = OUT / f"{ap}_phase_c_post{post}.tsv"
    camp.start_receiver(
        f"adaptive_c_{ap}_post{post}", session, tsv_p, OUT / f"{ap}_post{post}_rx.log"
    )
    camp.find_port(90)
    camp.flash(erase=False)
    t0 = time.time()
    while time.time() - t0 < 20 * 60:
        st = camp.analyze_tsv(tsv_p)
        camp.log(f"POST {post} hot={st.get('hot', 0)}/30")
        if st.get("hot", 0) >= 28:
            break
        time.sleep(5)
    st = camp.analyze_tsv(tsv_p)
    results.setdefault("phase_c_post", []).append({"post_ms": post, "delivery": st})
    return st


def run_sleep(
    ap: str, wp: int, pre: int, post_winner: int, sleep_ms: int, results: dict
) -> dict:
    camp.cmake_configure(
        ap,
        "C",
        {
            "AE_PROBE_OUTER": "1",
            "AE_PROBE_HOT_PER_OUTER": "30",
            "AE_PROBE_PROFILE": str(wp),
            "AE_PROBE_PRE_MS": str(pre),
            "AE_PROBE_POST_MS": str(post_winner),
            "AE_PROBE_SLEEP_US": str(sleep_ms * 1000),
            "AE_PROBE_RUN_ID": str(100 + sleep_ms),
            "BENCH_CLIENT_ID": "reliability_full_v1",
            "AETHER_PREPARED_NONCE_RESERVE": "30",
        },
    )
    camp.ninja_build()
    session = OUT / f"{ap}_rx_session"
    tsv_s = OUT / f"{ap}_phase_c_sleep{sleep_ms}.tsv"
    camp.start_receiver(
        f"adaptive_c_{ap}_s{sleep_ms}",
        session,
        tsv_s,
        OUT / f"{ap}_s{sleep_ms}_rx.log",
    )
    camp.find_port(90)
    camp.flash(erase=False)
    t0 = time.time()
    while time.time() - t0 < 20 * 60:
        st = camp.analyze_tsv(tsv_s)
        camp.log(f"sleep {sleep_ms} hot={st.get('hot', 0)}/30")
        if st.get("hot", 0) >= 28:
            break
        time.sleep(5)
    st = camp.analyze_tsv(tsv_s)
    results.setdefault("phase_c_sleep", []).append({"sleep_ms": sleep_ms, "delivery": st})
    return st


def load_phase_b() -> dict:
    canonical = OUT / "chirkov_phase_b_canonical.json"
    if canonical.exists():
        return json.loads(canonical.read_text(encoding="utf-8"))
    nested = json.loads((OUT / "chirkov_b.json").read_text(encoding="utf-8"))
    if "phase_b_canonical" in nested:
        return nested["phase_b_canonical"]
    if "chirkov" in nested:
        return nested["chirkov"].get("phase_b_canonical", {})
    return nested


def main() -> int:
    if LOCK.exists():
        raise RuntimeError(f"lock exists: {LOCK}; another continuation may be running")
    LOCK.write_text(str(time.time()), encoding="utf-8")
    try:
        return _main()
    finally:
        LOCK.unlink(missing_ok=True)


def _main() -> int:
    results = {
        "phase_a": json.loads((OUT / "chirkov_phase_a.json").read_text(encoding="utf-8")),
        "phase_b_canonical": load_phase_b(),
    }
    b_all = OUT / "chirkov_b.json"
    if b_all.exists():
        nested = json.loads(b_all.read_text(encoding="utf-8"))
        sel = nested.get("chirkov", nested).get("phase_b_selected_profile")
        if sel:
            results["phase_b_selected_profile"] = sel
    a = results["phase_a"]
    wp = max(0, int(a.get("winner_profile", 0)))
    pre = max(50, int(a.get("pre_ms", 300)))

    base = camp.analyze_tsv(OUT / "chirkov_phase_c.tsv")
    camp.log(f"baseline hot={base.get('hot')} full={base.get('full')}")
    results["phase_c_baseline"] = {
        "profile": wp,
        "pre_ms": pre,
        "post_ms": 300,
        "delivery": base,
    }

    post_winner = 300
    results["phase_c_post"] = []
    post200 = OUT / "chirkov_phase_c_post200.tsv"
    if post200.exists():
        st200 = camp.analyze_tsv(post200)
        results["phase_c_post"].append({"post_ms": 200, "delivery": st200})
        camp.log(f"POST 200 cached hot={st200.get('hot')}")
        if st200.get("hot", 0) >= 28:
            post_winner = 200

    for post in (100, 50, 25, 10, 0):
        st = run_post(AP, wp, pre, post, results)
        if st.get("hot", 0) >= 28:
            post_winner = post
        else:
            camp.log(f"POST {post} FAIL hot={st.get('hot')}; stop search")
            break
    results["phase_c_post_winner"] = post_winner
    camp.log(f"post_winner={post_winner}")

    results["phase_c_sleep"] = []
    sleep_remaining: list[int] = []
    for sleep_ms in (250, 500, 1000):
        tsv_s = OUT / f"chirkov_phase_c_sleep{sleep_ms}.tsv"
        if tsv_s.exists():
            st = camp.analyze_tsv(tsv_s)
            if st.get("hot", 0) >= 28:
                results["phase_c_sleep"].append({"sleep_ms": sleep_ms, "delivery": st})
                camp.log(f"sleep {sleep_ms} cached hot={st.get('hot')}")
                continue
            camp.log(f"sleep {sleep_ms} incomplete hot={st.get('hot')}; re-run")
        sleep_remaining.append(sleep_ms)

    for sleep_ms in sleep_remaining:
        run_sleep(AP, wp, pre, post_winner, sleep_ms, results)

    camp.cmake_configure(
        AP,
        "C",
        {
            "AE_PROBE_OUTER": "10",
            "AE_PROBE_HOT_PER_OUTER": "50",
            "AE_PROBE_PROFILE": str(wp),
            "AE_PROBE_PRE_MS": str(pre),
            "AE_PROBE_POST_MS": str(post_winner),
            "AE_PROBE_SLEEP_US": "1000000",
            "AE_PROBE_RUN_ID": "500",
            "BENCH_CLIENT_ID": "reliability_full_v1",
            "AETHER_PREPARED_NONCE_RESERVE": "50",
        },
    )
    camp.ninja_build()
    session = OUT / f"{AP}_rx_session"
    tsv_l = OUT / f"{AP}_phase_c_long.tsv"
    camp.start_receiver(f"adaptive_c_{AP}_long", session, tsv_l, OUT / f"{AP}_long_rx.log")
    camp.find_port(90)
    camp.flash(erase=False)
    t0 = time.time()
    while time.time() - t0 < 3 * 60 * 60:
        st = camp.analyze_tsv(tsv_l)
        camp.log(f"long hot={st.get('hot', 0)}/500")
        if st.get("hot", 0) >= 500:
            break
        time.sleep(15)
    results["phase_c_long"] = camp.analyze_tsv(tsv_l)
    camp.kill_receiver()

    (OUT / "chirkov_all.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    camp.log("chirkov C continuation done")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        camp.log(f"FATAL: {exc}")
        raise
