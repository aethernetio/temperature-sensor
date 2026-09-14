#!/usr/bin/env python3
"""Combined: historical UDP RTC Wi-Fi + BoardPowerDownForDeepSleep, 10x60s."""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402
import run_thermometer2_udp_low_power_1min as lp  # noqa: E402
import run_prepared_power_factor_study as pf  # noqa: E402
import _diag_udp_rtc_index_flash as flash_mod  # noqa: E402

BUILD = ROOT / "build-udp-lowenergy-plus-sleep"
OUT_DIR = HERE / "power_factor_results" / "thermometer2_low_energy_plots"
RAW_DIR = HERE / "power_modes_raw" / "thermometer2_combined_plus_sleep"
JSON_HIST = HERE / "power_factor_results" / "thermometer2_historical_udp_rtc_repro.json"
JSON_OUT = HERE / "power_factor_results" / "thermometer2_low_energy_wifi_plus_sleep.json"
MD_OUT = HERE / "THERMOMETER2_LOW_ENERGY_WIFI_PLUS_SLEEP.md"

UDP_HOST = "192.168.68.84"
UDP_PORT = 9000
VOLTAGE_MV = 3000
MEASURED = 10
PERIOD_S = 60.0
# warmup ~8s + 10*60 + 20s post-sleep margin
CAPTURE_S = 8 + 10 * 60 + 25
DT_S = 20 * 10e-6  # decim 20 @ 100 kHz
CR2_MAH = 800.0


def configure_and_build() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    wifi = pf.camp.APS["chirkov"]
    b = BUILD.as_posix()
    defs = (
        f"-B '{b}' "
        f"-D AETHER_DIAG_UDP_LOW_POWER_1MIN_10=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={wifi['ssid']} "
        f"-D WIFI_PASSWORD={wifi['password']} "
        f"-D AE_UDP_SERVER_HOST={UDP_HOST} "
        f"-D AE_UDP_SERVER_PORT={UDP_PORT} "
        f"-D AE_UDP_PRE_SETTLE_MS=50 "
        f"-D AE_UDP_POST_SEND_HOLD_MS=200"
    )
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} build
"""
    print("idf.py build combined", flush=True)
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"build failed code={r.returncode}")
    sdk = BUILD / "sdkconfig"
    if sdk.exists():
        before = sdk.read_text(encoding="utf-8", errors="replace")
        flash_mod.force_sdk(sdk)
        # Prefer silent console for sleep current (does not change Wi-Fi path).
        text = sdk.read_text(encoding="utf-8", errors="replace")
        bset = pf._set_kconfig_bool
        text = bset(text, "CONFIG_ESP_CONSOLE_UART_DEFAULT", False)
        text = bset(text, "CONFIG_ESP_CONSOLE_NONE", True)
        sdk.write_text(text, encoding="utf-8")
        after = sdk.read_text(encoding="utf-8", errors="replace")
        if before != after:
            print("force_sdk/console changed; rebuild", flush=True)
            r2 = sb.run(sb.idf_cmd(f"idf.py {defs} build"), cwd=str(ROOT))
            if r2.returncode != 0:
                raise RuntimeError(f"rebuild failed code={r2.returncode}")


def envelope_wakes(ua: np.ndarray, t: np.ndarray) -> list[tuple[float, float]]:
    step = 50
    u = ua[::step]
    tt = t[::step]
    active = u > 1500.0
    changes = np.diff(active.astype(int))
    starts = np.where(changes == 1)[0] + 1
    ends = np.where(changes == -1)[0] + 1
    if active[0]:
        starts = np.r_[0, starts]
    if active[-1]:
        ends = np.r_[ends, len(active) - 1]
    segs = []
    for s, e in zip(starts, ends):
        dur = (e - s) * 0.01
        if dur >= 0.15:
            a = max(0.0, float(tt[s]) - 0.08)
            b = min(float(t[-1]), float(tt[min(e, len(tt) - 1)]) + 0.20)
            segs.append((a, b))
    return segs


def analyze(csv: Path, out_prefix: str) -> dict:
    ua = np.loadtxt(csv, delimiter=",", skiprows=1, usecols=1)
    t = np.arange(len(ua), dtype=float) * DT_S
    segs = envelope_wakes(ua, t)
    # drop power-on / warmup (first), take next 10
    wakes = segs[1 : 1 + MEASURED] if len(segs) > MEASURED else segs[:MEASURED]
    rows = []
    for i, (a, b) in enumerate(wakes):
        m = (t >= a) & (t <= b)
        q = float(np.trapezoid(ua[m] * 1e-6, t[m])) * 1000.0
        rows.append(
            {
                "i": i,
                "t_start_s": a,
                "t_end_s": b,
                "duration_ms": (b - a) * 1000.0,
                "charge_mC": q,
            }
        )
    qs = [r["charge_mC"] for r in rows]
    ds = [r["duration_ms"] for r in rows]
    # sleep after last wake (prefer last 15s of capture if past last wake)
    if wakes:
        t_sleep0 = wakes[-1][1] + 1.0
    else:
        t_sleep0 = max(0.0, t[-1] - 15.0)
    sleep_m = (t >= t_sleep0) & (ua < 200.0)
    sleep_avg = float(np.mean(ua[sleep_m])) if np.any(sleep_m) else None
    sleep_med = float(np.median(ua[sleep_m])) if np.any(sleep_m) else None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 5))
    st = max(1, len(t) // 200000)
    ax.plot(t[::st], ua[::st], lw=0.35)
    for i, (a, b) in enumerate(wakes):
        ax.axvspan(a, b, color="orange", alpha=0.3)
        ax.text((a + b) / 2, 2e5, str(i), ha="center", fontsize=8)
    ax.set_yscale("symlog", linthresh=100)
    ax.set_title(f"{out_prefix} — 10 wakes")
    ax.set_xlabel("t (s)")
    ax.set_ylabel("I (uA)")
    fig.tight_layout()
    overview = OUT_DIR / f"{out_prefix}_10_wakes_overview.png"
    fig.savefig(overview, dpi=120)
    plt.close(fig)
    zooms = []
    for i, (a, b) in enumerate(wakes):
        pad = max(0.4, (b - a) * 0.6)
        m = (t >= a - pad) & (t <= b + pad)
        fig, ax = plt.subplots(figsize=(10, 4))
        ts, us = t[m], ua[m]
        st = max(1, len(ts) // 80000)
        ax.plot(ts[::st], us[::st], lw=0.6)
        ax.axvline(a, color="g", ls="--")
        ax.axvline(b, color="r", ls="--")
        ax.axvspan(a, b, color="orange", alpha=0.2)
        ax.set_yscale("symlog", linthresh=100)
        ax.set_title(
            f"Wake {i}: {rows[i]['duration_ms']:.0f} ms, {rows[i]['charge_mC']:.1f} mC"
        )
        fig.tight_layout()
        z = OUT_DIR / f"{out_prefix}_wake_{i:02d}_zoom.png"
        fig.savefig(z, dpi=120)
        plt.close(fig)
        zooms.append(str(z))

    mean_mC = statistics.mean(qs) if qs else None
    med_mC = statistics.median(qs) if qs else None
    # 1-min cycle charge = active + sleep*(60-dur)
    cycle_mCs = []
    if mean_mC is not None and sleep_avg is not None:
        for r in rows:
            sleep_s = max(0.0, PERIOD_S - r["duration_ms"] / 1000.0)
            cycle_mCs.append(r["charge_mC"] + sleep_avg * 1e-6 * sleep_s * 1000.0)
    return {
        "wakes": rows,
        "summary": {
            "n": len(qs),
            "mean_mC": mean_mC,
            "median_mC": med_mC,
            "min_mC": min(qs) if qs else None,
            "max_mC": max(qs) if qs else None,
            "mean_duration_ms": statistics.mean(ds) if ds else None,
            "median_duration_ms": statistics.median(ds) if ds else None,
        },
        "sleep_uA": {"avg": sleep_avg, "median": sleep_med, "from_s": t_sleep0},
        "cycle_mC": {
            "mean": statistics.mean(cycle_mCs) if cycle_mCs else None,
            "median": statistics.median(cycle_mCs) if cycle_mCs else None,
        },
        "plots": {"overview": str(overview), "zooms": zooms},
        "segs_total": len(segs),
    }


def write_md(hist: dict, comb: dict, meta: dict) -> None:
    hs = hist.get("summary") or {}
    cs = comb.get("summary") or {}
    sleep = comb.get("sleep_uA") or {}
    cycle = comb.get("cycle_mC") or {}
    mean_active = cs.get("mean_mC") or 0.0
    sleep_avg = sleep.get("avg") or 0.0
    sleep_mC_min = sleep_avg * 1e-6 * 60.0 * 1000.0
    total_mC = (cycle.get("mean") if cycle.get("mean") is not None else mean_active + sleep_mC_min)
    i_avg_uA = (total_mC / 1000.0) / 60.0 * 1e6
    life_h = (CR2_MAH * 1000.0) / max(i_avg_uA, 1e-9)
    life_d = life_h / 24.0
    lines = [
        "# Thermometer 2 — low-energy Wi-Fi + 7 µA sleep",
        "",
        f"- Branch: `{meta['branch']}`",
        f"- SHA: `{meta['sha']}`",
        f"- Parent/historical repro SHA: `{meta.get('parent_sha')}`",
        f"- Voltage: {VOLTAGE_MV} mV",
        f"- UDP: `{UDP_HOST}:{UDP_PORT}`",
        "",
        "## 1. Historical low-energy source",
        "",
        "```",
        (hist.get("HISTORICAL_LOW_ENERGY_SOURCE") or hist.get("historical_source") or ""),
        "```",
        "",
        "- Closest documented numeric match also: LONG1000_CHIRKOV prepared HOT",
        "  (~56.8 mC / ~572 ms) on feat/prepared-power-factor-study.",
        "- Thermometer2 UDP path reproduced here: `AETHER_DIAG_UDP_RTC_INDEX`",
        "  with `force_sdk` from `_diag_udp_rtc_index_flash.py`, PRE=50 POST=200,",
        "  10 s interval, full RTC cache, Wi-Fi4+1M, FULL teardown, **no** BoardPowerDown.",
        "",
        "## 2. Exact reproduction (A)",
        "",
        f"| Q | start s | end s | dur ms | mC |",
        f"|---|---:|---:|---:|---:|",
    ]
    for r in hist.get("wakes") or []:
        lines.append(
            f"| {r['i']+1} | {r['t_start_s']:.3f} | {r['t_end_s']:.3f} | "
            f"{r['duration_ms']:.0f} | {r['charge_mC']:.2f} |"
        )
    lines += [
        "",
        f"- RX: {hist.get('udp_rx_in_capture', 'see capture log')}",
        f"- mean/median/min/max mC: "
        f"{hs.get('mean_mC')}/{hs.get('median_mC')}/{hs.get('min_mC')}/{hs.get('max_mC')}",
        f"- median duration ms: {hs.get('median_duration_ms')}",
        f"- acceptance 40–80 mC: **{hist.get('acceptance_40_80_mC')}**",
        "",
        "## 3. Visual PPK boundary method",
        "",
        "- Reconstruct sample time at 5 kHz (decim 20 from 100 kHz).",
        "- 100 Hz envelope: active if I>1.5 mA for ≥150 ms.",
        "- Pad −80 ms / +200 ms for rise + teardown tail into deep-sleep floor.",
        "- Do **not** stop at sendto / wifi_stop; include visible tail.",
        f"- Overview: `{ (hist.get('plots') or {}).get('overview') }`",
        "",
        "## 4. Sleep cleanup-only diff",
        "",
        "- Same Wi-Fi/UDP algorithm (PRE=50, POST=200, caches, PS, teardown, 1M).",
        "- Added/kept `BoardPowerDownForDeepSleep()` before `esp_deep_sleep_start`.",
        "- Interval changed only for final run: 60 s start-to-start, 10 measured sends.",
        "- Default PRE for `AETHER_DIAG_UDP_LOW_POWER_1MIN_10` set to 50 (was 300).",
        "- RTC_DATA_ATTR caches retained; console NONE for sleep measurement.",
        "",
        "## 5. Final combined 10-send run (B)",
        "",
        f"| Q | start s | end s | dur ms | mC |",
        f"|---|---:|---:|---:|---:|",
    ]
    for r in comb.get("wakes") or []:
        lines.append(
            f"| {r['i']+1} | {r['t_start_s']:.3f} | {r['t_end_s']:.3f} | "
            f"{r['duration_ms']:.0f} | {r['charge_mC']:.2f} |"
        )
    lines += [
        "",
        f"- mean/median active mC: {cs.get('mean_mC')}/{cs.get('median_mC')}",
        f"- sleep avg/median µA: {sleep.get('avg')}/{sleep.get('median')}",
        f"- mean charge per 1-min cycle mC: {cycle.get('mean')}",
        f"- overview: `{(comb.get('plots') or {}).get('overview')}`",
        "",
        "## 6. Delivery",
        "",
        f"- Combined UDP summary: {json.dumps(comb.get('udp') or {}, ensure_ascii=True)}",
        "",
        "## 7. Battery estimate (CR2 800 mAh @ 3.0 V)",
        "",
        f"- active mean mC/send: {mean_active}",
        f"- sleep µA: {sleep_avg}",
        f"- total mC / 1 min: {total_mC}",
        f"- I_avg µA: {i_avg_uA:.2f}",
        f"- life: {life_d:.1f} d / {life_d/30.0:.2f} mo",
        "",
        "## A/B",
        "",
        "| Metric | A old Wi-Fi | B + low-power sleep |",
        "|---|---:|---:|",
        f"| RX | see hist | {(comb.get('udp') or {}).get('rx_unique')} |",
        f"| active mean mC | {hs.get('mean_mC')} | {cs.get('mean_mC')} |",
        f"| active median mC | {hs.get('median_mC')} | {cs.get('median_mC')} |",
        f"| wake duration mean ms | {hs.get('mean_duration_ms')} | {cs.get('mean_duration_ms')} |",
        f"| sleep current µA | {(hist.get('sleep_between_wakes_uA') or {}).get('avg')} | {sleep.get('avg')} |",
        "",
    ]
    MD_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-flash", action="store_true")
    ap.add_argument("--analyze-only", type=Path, default=None)
    args = ap.parse_args()

    branch, sha = sb.git_meta()
    hist = json.loads(JSON_HIST.read_text(encoding="utf-8")) if JSON_HIST.exists() else {}

    if args.analyze_only:
        comb = analyze(args.analyze_only, "combined")
        out = {
            "branch": branch,
            "sha": sha,
            "historical": hist,
            "combined": comb,
            "utc": datetime.now(timezone.utc).isoformat(),
        }
        JSON_OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
        write_md(hist, comb, {"branch": branch, "sha": sha, "parent_sha": hist.get("sha")})
        print(json.dumps(comb["summary"], indent=2), flush=True)
        return 0

    if not args.skip_build:
        configure_and_build()

    lp.kill_udp_9000()
    udp = lp.UdpCollector()
    subprocess.run(
        [
            "powershell",
            "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ppk2_(hold|log|integrate)' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        capture_output=True,
    )
    time.sleep(1)
    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    time.sleep(0.5)
    if not args.skip_flash:
        print("=== FLASH combined ===", flush=True)
        port = sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=8.0)
        print(f"flashed port={port}", flush=True)
    else:
        ppk.power_cycle()
        time.sleep(2.0)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_csv = RAW_DIR / f"ppk_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    assert ppk.ppk is not None
    ppk.ppk.start_measuring()
    t0 = time.time()
    rows_t: list[float] = []
    rows_u: list[float] = []
    decim = 20
    buf_i = 0
    last_status = t0
    print(f"=== CAPTURE {CAPTURE_S}s -> {raw_csv} ===", flush=True)
    try:
        while time.time() - t0 < CAPTURE_S:
            udp.poll()
            try:
                raw = ppk.ppk.get_data()
            except OSError as e:
                print(f"ppk_warn={e}", flush=True)
                time.sleep(0.01)
                continue
            if not raw:
                time.sleep(0.001)
                continue
            try:
                chunk, _ = ppk.ppk.get_samples(raw)
            except Exception:
                continue
            now = time.time() - t0
            for s in chunk:
                buf_i += 1
                if buf_i % decim != 0:
                    continue
                rows_t.append(now)
                rows_u.append(float(s))
            if time.time() - last_status > 10:
                print(
                    f"ppk t={now:.1f}s n={len(rows_t)} udp_rx={len(udp.events)}",
                    flush=True,
                )
                last_status = time.time()
            # early stop after measured idx 10 seen + 20s sleep
            idxs = [e["idx"] for e in udp.events]
            if 10 in idxs:
                # wait 20s of sleep after last idx10
                t10 = max(e["t"] for e in udp.events if e["idx"] == 10)
                if time.time() - t10 >= 20.0:
                    print("early_stop after #10 + 20s sleep", flush=True)
                    break
    finally:
        try:
            ppk.ppk.stop_measuring()
        except Exception:
            pass
        udp_sum = udp.summary()
        udp.close()

    with raw_csv.open("w", encoding="utf-8", newline="") as f:
        f.write("t_s,uA\n")
        for tt, uu in zip(rows_t, rows_u):
            f.write(f"{tt:.6f},{uu:.3f}\n")

    comb = analyze(raw_csv, "combined")
    comb["udp"] = udp_sum
    comb["ppk_csv"] = str(raw_csv)
    out = {
        "branch": branch,
        "sha": sha,
        "parent_sha": "14118bac5570a2bba0d311197799faed0a63b250",
        "voltage_mv": VOLTAGE_MV,
        "pre_ms": 50,
        "post_ms": 200,
        "period_s": 60,
        "board_powerdown": True,
        "historical": hist,
        "combined": comb,
        "utc": datetime.now(timezone.utc).isoformat(),
    }
    JSON_OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    write_md(
        hist,
        comb,
        {"branch": branch, "sha": sha, "parent_sha": out["parent_sha"]},
    )
    print(json.dumps(comb["summary"], indent=2), flush=True)
    print(json.dumps(comb["sleep_uA"], indent=2), flush=True)
    print(f"wrote {JSON_OUT} {MD_OUT}", flush=True)
    try:
        if ppk.ppk and ppk.ppk.ser and ppk.ppk.ser.is_open:
            ppk.ppk.ser.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
