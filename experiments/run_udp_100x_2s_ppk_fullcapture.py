#!/usr/bin/env python3
"""UDP 100x / 2 s / cached firmware — full PPK capture with UDP-independent segmentation."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import socket
import statistics
import struct
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402
from ppk_stream_cycle_segmenter import (
    CloseReason,
    CycleWindow,
    SegmenterConfig,
    StreamingCycleSegmenter,
)  # noqa: E402

BUILD = ROOT / "build-udp-100x-2s-ppk"
RESULTS = HERE / "power_factor_results"
LOCAL_RAW = RESULTS / "udp_100x_2s_ppk_fullcapture_raw"
WINDOWS_DIR = LOCAL_RAW / "windows"
CSV_OUT = RESULTS / "udp_100x_2s_ppk_fullcapture_sends.csv"
JSON_OUT = RESULTS / "udp_100x_2s_ppk_fullcapture.json"
MD_OUT = HERE / "UDP_100X_2S_PPK_CACHED_FULLCAPTURE.md"

UDP_PORT = 9000
PRE_SETTLE_MS = 50
POST_HOLD_MS = 200
MEASURED = 100
PERIOD_S = 2
VOLTAGE_MV = 3000
VOLTAGE_V = VOLTAGE_MV / 1000.0
FS_HZ = 100_000.0
GUARD_SAMPLES = int(0.75 * FS_HZ)
COLD_BOOT_S = 60.0
GAP_INVALID_SAMPLES = 500

START_BRANCH = ""
START_SHA = ""


def wifi_creds() -> tuple[str, str]:
    return "chirkov", "kcdjepWz51"


def detect_lan_host() -> str:
    for probe in ("192.168.68.1", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 1 if probe.endswith(".1") else 80))
            return s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("could not detect LAN IPv4")


def kill_udp_port() -> None:
    if sys.platform != "win32":
        return
    r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, check=False)
    for line in (r.stdout or "").splitlines():
        if f":{UDP_PORT}" in line and "UDP" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = int(parts[-1])
                if pid not in (0, os.getpid()):
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    return float(np.percentile(np.asarray(vals, dtype=float), p))


def agg_stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "p50": None, "p90": None, "p95": None, "min": None, "max": None, "stddev": None}
    return {
        "n": len(vals),
        "mean": float(statistics.mean(vals)),
        "median": float(statistics.median(vals)),
        "p50": pct(vals, 50),
        "p90": pct(vals, 90),
        "p95": pct(vals, 95),
        "min": float(min(vals)),
        "max": float(max(vals)),
        "stddev": float(statistics.stdev(vals)) if len(vals) > 1 else 0.0,
    }


def git_meta() -> tuple[str, str]:
    b = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, cwd=ROOT, check=False)
    s = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=False)
    return (b.stdout or "").strip(), (s.stdout or "").strip()


def build_firmware(udp_host: str) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, password = wifi_creds()
    b = BUILD.as_posix()
    defs = (
        f"-D AETHER_DIAG_UDP_LOW_POWER_1MIN_10=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-D AE_UDP_SERVER_HOST={udp_host} "
        f"-D AE_UDP_SERVER_PORT={UDP_PORT} "
        f"-D AE_UDP_PRE_SETTLE_MS={PRE_SETTLE_MS} "
        f"-D AE_UDP_POST_SEND_HOLD_MS={POST_HOLD_MS} "
        f"-D AE_UDP_MEASURED_SENDS={MEASURED} "
        f"-D AE_UDP_WIFI_CACHE=1 "
        f"-D AE_UDP_PERIOD_S={PERIOD_S} "
        f"-B '{b}'"
    )
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} build
"""
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"build failed code={r.returncode}")


class UdpCollector:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", UDP_PORT))
        self.sock.setblocking(False)
        self.events: list[dict] = []

    def poll(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(64)
            except BlockingIOError:
                break
            except OSError:
                break
            if len(data) < 4:
                continue
            (idx,) = struct.unpack_from("<I", data, 0)
            self.events.append(
                {"t": time.time(), "idx": int(idx), "src_ip": addr[0], "dup_n": 0}
            )
            print(f"UDP idx={idx} from={addr[0]}", flush=True)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def rx_map(self, t0_wall: float) -> dict[int, dict]:
        """First receive time per sequence (measured 1..100)."""
        by: dict[int, list] = {}
        for e in self.events:
            idx = e["idx"]
            if idx < 1 or idx > MEASURED:
                continue
            by.setdefault(idx, []).append(e)
        out: dict[int, dict] = {}
        for idx, evs in by.items():
            first = evs[0]
            out[idx] = {
                "received": True,
                "t_wall": first["t"],
                "t_rel_capture": first["t"] - t0_wall,
                "src_ip": first["src_ip"],
                "duplicate_count": max(0, len(evs) - 1),
            }
        return out


class ContinuousPpkCapture:
    def __init__(self, ppk: sb.PpkSession, raw_bin: Path, meta_path: Path) -> None:
        self.ppk = ppk
        self.raw_bin = raw_bin
        self.meta_path = meta_path
        self.bin_f = raw_bin.open("wb")
        self.wall_start = 0.0
        self.sample_count = 0
        self.chunk_gaps: list[int] = []
        self.last_end_sample = -1
        self.cfg = SegmenterConfig(fs_hz=FS_HZ, cold_boot_ignore_s=COLD_BOOT_S, max_measured=MEASURED)
        self.segmenter = StreamingCycleSegmenter(self.cfg)
        self.segmenter.on_window = self._on_window
        self.ring: deque[float] = deque(maxlen=int(55 * FS_HZ))

    def _on_window(self, w: CycleWindow) -> None:
        print(
            f"PPK_WINDOW seq={w.sequence} start={w.wake_start_s:.3f}s "
            f"end={w.wake_end_s:.3f}s dur_ms={(w.wake_end_s - w.wake_start_s) * 1e3:.0f}",
            flush=True,
        )

    def start(self) -> None:
        assert self.ppk.ppk is not None
        self.wall_start = time.time()
        self.ppk.set_on()
        self.ppk.ppk.start_measuring()

    def stop(self) -> None:
        assert self.ppk.ppk is not None
        self.segmenter.finalize()
        try:
            self.ppk.ppk.stop_measuring()
        except Exception as e:
            print(f"ppk_stop_warn={e}", flush=True)
        self.bin_f.close()
        meta = {
            "fs_hz": FS_HZ,
            "wall_start_utc": datetime.fromtimestamp(self.wall_start, tz=timezone.utc).isoformat(),
            "sample_count": self.sample_count,
            "voltage_mV": VOLTAGE_MV,
            "chunk_gaps_samples": self.chunk_gaps,
            "raw_bin": str(self.raw_bin),
        }
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def drain(self) -> None:
        assert self.ppk.ppk is not None
        try:
            raw = self.ppk.ppk.get_data()
        except OSError as e:
            print(f"ppk_get_data_warn={e}", flush=True)
            return
        if not raw:
            return
        try:
            chunk, _ = self.ppk.ppk.get_samples(raw)
        except Exception as e:
            print(f"ppk_get_samples_warn={e}", flush=True)
            return
        if not chunk:
            return
        start_idx = self.sample_count
        if self.last_end_sample >= 0 and start_idx > self.last_end_sample + 1:
            gap = start_idx - self.last_end_sample - 1
            self.chunk_gaps.append(gap)
        arr = np.asarray(chunk, dtype=np.float32)
        arr.tofile(self.bin_f)
        self.sample_count += len(arr)
        self.last_end_sample = self.sample_count - 1
        self.segmenter.feed_chunk(arr)
        for v in arr:
            self.ring.append(float(v))


def resegment_offline(raw_bin: Path) -> tuple[int, list[CycleWindow]]:
    u = np.fromfile(raw_bin, dtype=np.float32)
    seg = StreamingCycleSegmenter(SegmenterConfig())
    chunk = 100_000
    for i in range(0, len(u), chunk):
        seg.feed_chunk(u[i : i + chunk])
    seg.finalize()
    measured = len([w for w in seg.windows_closed if 1 <= w.sequence <= MEASURED])
    return measured, seg.windows_closed


def load_raw_slice(raw_bin: Path, start: int, end: int) -> tuple[np.ndarray, np.ndarray]:
    count = max(0, end - start)
    if count <= 0:
        return np.array([]), np.array([])
    with raw_bin.open("rb") as f:
        f.seek(start * 4)
        u = np.fromfile(f, dtype=np.float32, count=count)
    t = (np.arange(start, start + len(u), dtype=np.float64)) / FS_HZ
    return t, u.astype(np.float64)


def integrate_window(raw_bin: Path, w: CycleWindow) -> dict:
    g0 = max(0, w.wake_start_sample - GUARD_SAMPLES)
    g1 = min(w.wake_end_sample + GUARD_SAMPLES, w.wake_end_sample + int(FS_HZ))
    # read full raw for wake..end only for charge (guards stored separately in file)
    t, u = load_raw_slice(raw_bin, w.wake_start_sample, w.wake_end_sample)
    tg, ug = load_raw_slice(raw_bin, g0, g1)
    invalid_reason = ""
    valid = True
    if w.max_gap_samples >= GAP_INVALID_SAMPLES:
        valid = False
        invalid_reason = "INVALID_ACQUISITION_GAP"
    if len(t) < 100:
        valid = False
        invalid_reason = invalid_reason or "too_few_samples"
    close_reason = getattr(w, "close_reason", CloseReason.SLEEP_EDGE)
    if close_reason == CloseReason.FORCE_LIMIT:
        valid = False
        invalid_reason = invalid_reason or "force_limit_clip"
    elif close_reason == CloseReason.FINALIZE:
        valid = False
        invalid_reason = invalid_reason or "finalize_no_sleep_edge"
    duration_s = w.wake_end_s - w.wake_start_s
    charge_mC = 0.0
    peak_mA = 0.0
    avg_mA = 0.0
    if len(t) >= 2:
        try:
            integ = float(np.trapezoid(u, t))
        except AttributeError:
            integ = float(np.trapz(u, t))
        charge_mC = abs(integ) / 1000.0
        peak_mA = float(np.max(np.abs(u))) / 1000.0
        avg_mA = (charge_mC / duration_s) if duration_s > 0 else 0.0
    expected = int(duration_s * FS_HZ)
    actual = len(t)
    gap = max(0, expected - actual)
    WINDOWS_DIR.mkdir(parents=True, exist_ok=True)
    win_path = WINDOWS_DIR / f"seq_{w.sequence:03d}.bin"
    if len(tg):
        tg.astype(np.float32).tofile(win_path)
    return {
        "sequence": w.sequence,
        "wake_start_s": w.wake_start_s,
        "wake_end_s": w.wake_end_s,
        "duration_ms": duration_s * 1000.0,
        "charge_mC": charge_mC,
        "energy_mJ": charge_mC * VOLTAGE_V,
        "average_current_mA": avg_mA,
        "peak_current_mA": peak_mA,
        "expected_samples": expected,
        "actual_samples": actual,
        "missing_samples": gap,
        "max_gap_samples": w.max_gap_samples,
        "acquisition_gap": w.max_gap_samples >= GAP_INVALID_SAMPLES,
        "valid": valid,
        "invalid_reason": invalid_reason,
        "close_reason": str(close_reason.value),
        "raw_window_path": str(win_path),
        "guard_start_sample": g0,
    }


def build_rows(windows: list[CycleWindow], raw_bin: Path, rx: dict[int, dict], t0_wall: float) -> list[dict]:
    ordered = sorted(windows, key=lambda w: w.wake_start_s)
    if ordered and ordered[0].sequence == 0:
        measured = ordered[1 : MEASURED + 1]
    else:
        measured = ordered[:MEASURED]
    rows: list[dict] = []
    prev_wake: float | None = None
    prev_end: float | None = None
    for seq_i, w in enumerate(measured, start=1):
        base = integrate_window(raw_bin, w)
        base["sequence"] = seq_i
        rxinfo = rx.get(seq_i, {"received": False, "duplicate_count": 0, "src_ip": "", "t_rel_capture": None})
        sts = None
        if prev_wake is not None:
            sts = w.wake_start_s - prev_wake
        sleep_d = None
        if prev_end is not None:
            sleep_d = w.wake_start_s - prev_end
        row = {
            **base,
            "start_to_start_s": sts,
            "sleep_duration_s": sleep_d,
            "udp_received": bool(rxinfo.get("received")),
            "duplicate_count": int(rxinfo.get("duplicate_count", 0)),
            "udp_src_ip": rxinfo.get("src_ip", ""),
            "udp_t_rel_s": rxinfo.get("t_rel_capture"),
        }
        rows.append(row)
        prev_wake = w.wake_start_s
        prev_end = w.wake_end_s
    return rows


def run_capture(
    ppk: sb.PpkSession,
    udp: UdpCollector,
    raw_bin: Path,
    meta_path: Path,
    stop_after_measured: int | None,
    label: str,
) -> ContinuousPpkCapture:
    cap = ContinuousPpkCapture(ppk, raw_bin, meta_path)
    cap.start()
    t0 = time.time()
    if stop_after_measured is not None:
        deadline = t0 + COLD_BOOT_S + (stop_after_measured + 2) * 14 + 90
    else:
        deadline = t0 + COLD_BOOT_S + MEASURED * 6 + 180
    print(f"=== CAPTURE {label} deadline~={deadline - t0:.0f}s ===", flush=True)
    t_udp100: float | None = None
    while time.time() < deadline:
        udp.poll()
        cap.drain()
        for e in udp.events:
            if e["idx"] >= MEASURED and t_udp100 is None:
                t_udp100 = time.time()
        closed = cap.segmenter.windows_closed
        mw = cap.segmenter.measured_windows
        if stop_after_measured is not None:
            if mw >= stop_after_measured and any(w.sequence >= stop_after_measured for w in closed):
                time.sleep(1.5)
                cap.drain()
                break
        elif t_udp100 is not None and (time.time() - t_udp100) > 12.0:
            time.sleep(1.0)
            cap.drain()
            break
        elif mw >= MEASURED and any(w.sequence >= MEASURED for w in closed):
            time.sleep(2.5)
            cap.drain()
            break
        time.sleep(0.001)
    cap.stop()
    print(
        f"CAPTURE {label} windows={cap.segmenter.total_windows_including_warmup} "
        f"measured={cap.segmenter.measured_windows} samples={cap.sample_count}",
        flush=True,
    )
    return cap


def delivery_only(
    ppk: sb.PpkSession, udp_host: str, label: str, *, reflash: bool = True
) -> dict:
    kill_udp_port()
    udp = UdpCollector()
    if reflash:
        print(f"=== {label} REFLASH (same binary, reset run counter) ===", flush=True)
        sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
        time.sleep(1.0)
    else:
        ppk.power_cycle()
    port = sb.wait_espressif_com(timeout_s=50.0)
    if port:
        try:
            sb.hard_reset(port)
        except Exception as e:
            print(f"reset_warn={e}", flush=True)
    t0 = time.time()
    seen: set[int] = set()
    deadline = t0 + COLD_BOOT_S + MEASURED * 15 + 120
    while time.time() < deadline:
        udp.poll()
        for e in udp.events:
            if 1 <= e["idx"] <= MEASURED:
                seen.add(e["idx"])
        if len(seen) >= MEASURED:
            break
        if max(seen, default=0) >= MEASURED and (time.time() - t0) > COLD_BOOT_S + MEASURED * 4:
            break
        time.sleep(0.01)
    udp.close()
    missing = [i for i in range(1, MEASURED + 1) if i not in seen]
    print(f"{label} RX={len(seen)}/{MEASURED} missing={missing[:10]}...", flush=True)
    return {"rx_unique": len(seen), "missing": missing}


def write_report(rep: dict) -> None:
    lines = [
        "# UDP 100x / 2 S / PPK CACHED FULL CAPTURE",
        "",
        "Previous 15/100 PPK result from UDP-gated segmentation is **superseded**.",
        "Segmentation is **independent of UDP RX**; start-to-start from PPK wake times.",
        "",
        f"- branch: `{rep.get('branch')}`",
        f"- sha: `{rep.get('sha')}`",
        f"- destination: `{rep.get('destination')}`",
        f"- wifi_cache: 1, period_s: 2, attempts: 100",
        "",
        f"- PPK measured windows detected: {rep.get('measured_windows_detected')}",
        f"- valid integrated: {rep.get('valid_windows')}",
        f"- PPK_RUN: {rep.get('PPK_RUN')}",
        "",
        f"- UDP unique RX: {rep.get('udp', {}).get('rx_unique')}/100",
        f"- delivery confirmation aggregate: {rep.get('delivery_confirmation', {}).get('aggregate_rx')}/300",
        "",
        f"- csv: `{CSV_OUT}`",
        f"- json: `{JSON_OUT}`",
    ]
    MD_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    global START_BRANCH, START_SHA
    ap = argparse.ArgumentParser()
    ap.add_argument("--udp-host", default="")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--validation-only", action="store_true")
    ap.add_argument("--final-only", action="store_true")
    ap.add_argument("--skip-delivery", action="store_true")
    ap.add_argument("--delivery-only", action="store_true")
    args = ap.parse_args()

    START_BRANCH, START_SHA = git_meta()
    print(f"START_BRANCH={START_BRANCH} START_SHA={START_SHA}", flush=True)

    udp_host = args.udp_host.strip() or detect_lan_host()
    print(f"udp_target={udp_host}:{UDP_PORT}", flush=True)

    RESULTS.mkdir(parents=True, exist_ok=True)
    LOCAL_RAW.mkdir(parents=True, exist_ok=True)

    if not args.skip_build and not args.final_only:
        print("=== BUILD ===", flush=True)
        build_firmware(udp_host)
    elif not (BUILD / "flash_args").exists():
        build_firmware(udp_host)

    kill_udp_port()
    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    branch, sha = git_meta()

    try:
        if args.delivery_only:
            run_a = delivery_only(ppk, udp_host, "DELIVERY_A", reflash=True)
            run_b = delivery_only(ppk, udp_host, "DELIVERY_B", reflash=True)
            print(f"DELIVERY_A={run_a['rx_unique']}/100 DELIVERY_B={run_b['rx_unique']}/100", flush=True)
            return 0

        if not args.final_only:
            print("=== FLASH ===", flush=True)
            sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
            time.sleep(1.0)
            port = sb.wait_espressif_com(timeout_s=25.0)
            if port:
                sb.hard_reset(port)

        # --- validation: warmup + 5 measured ---
        if not args.final_only:
            kill_udp_port()
            udp_v = UdpCollector()
            ppk.power_cycle()
            port = sb.wait_espressif_com(timeout_s=50.0)
            if port:
                sb.hard_reset(port)
            val_bin = LOCAL_RAW / "validation_uA_f32.bin"
            val_meta = LOCAL_RAW / "validation_meta.json"
            cap_v = run_capture(ppk, udp_v, val_bin, val_meta, stop_after_measured=5, label="validation")
            udp_v.close()
            warmups = [w for w in cap_v.segmenter.windows_closed if w.sequence == 0]
            measured5 = [w for w in cap_v.segmenter.windows_closed if 1 <= w.sequence <= 5]
            ok_val = len(warmups) >= 1 and len(measured5) >= 5
            print(f"VALIDATION windows_w0={len(warmups)} w1_5={len(measured5)} ok={ok_val}", flush=True)
            if not ok_val:
                print("VALIDATION FAIL", flush=True)
                return 2
            if args.validation_only:
                return 0

        # --- final ---
        kill_udp_port()
        udp = UdpCollector()
        ppk.power_cycle()
        port = sb.wait_espressif_com(timeout_s=50.0)
        if port:
            sb.hard_reset(port)
        final_bin = LOCAL_RAW / "final_uA_f32.bin"
        final_meta = LOCAL_RAW / "final_meta.json"
        cap = run_capture(ppk, udp, final_bin, final_meta, stop_after_measured=None, label="final")
        t0_wall = cap.wall_start
        rx = udp.rx_map(t0_wall)
        _, windows = resegment_offline(final_bin)
        ordered = sorted(windows, key=lambda w: w.wake_start_s)
        measured_detected = max(0, len(ordered) - (1 if ordered and ordered[0].sequence == 0 else 0))
        measured_detected = min(measured_detected, MEASURED)
        rows = build_rows(windows, final_bin, rx, t0_wall)
        if len(rows) != MEASURED:
            print(f"WARN row_count={len(rows)} expected={MEASURED}", flush=True)

        fieldnames = [
            "sequence", "wake_start_s", "wake_end_s", "duration_ms", "charge_mC", "energy_mJ",
            "average_current_mA", "peak_current_mA", "start_to_start_s", "sleep_duration_s",
            "udp_received", "duplicate_count", "acquisition_gap", "valid", "invalid_reason",
            "raw_window_path", "expected_samples", "actual_samples", "missing_samples", "max_gap_samples",
        ]
        with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow(row)

        valid_rows = [r for r in rows if r.get("valid") and r.get("sequence", 0) >= 1]
        times = [r["duration_ms"] for r in valid_rows]
        charges = [r["charge_mC"] for r in valid_rows]
        energies = [r["energy_mJ"] for r in valid_rows]
        sts = [r["start_to_start_s"] for r in rows[1:] if r.get("start_to_start_s") is not None]
        sleeps = [r["sleep_duration_s"] for r in rows[1:] if r.get("sleep_duration_s") is not None]

        rx_ok = [r for r in rows if r.get("udp_received")]
        rx_lost = [r for r in rows if not r.get("udp_received")]

        missing_udp = [r["sequence"] for r in rows if not r.get("udp_received")]
        udp_sum = {
            "attempts": MEASURED,
            "rx_unique": len(rx_ok),
            "missing": missing_udp,
            "duplicates": {str(k): v["duplicate_count"] for k, v in rx.items() if v["duplicate_count"]},
            "rx_pct": 100.0 * len(rx_ok) / MEASURED,
        }

        valid_n = len(valid_rows)
        ppk_run = "PASS" if measured_detected >= MEASURED and valid_n >= 95 else "FAIL"

        delivery = {}
        ppk_rx = len(rx_ok)
        if not args.skip_delivery:
            run_a = delivery_only(ppk, udp_host, "DELIVERY_A")
            run_b = delivery_only(ppk, udp_host, "DELIVERY_B")
            agg = ppk_rx + run_a["rx_unique"] + run_b["rx_unique"]
            delivery = {
                "PPK_RUN_RX": ppk_rx,
                "RUN_A_RX": run_a["rx_unique"],
                "RUN_B_RX": run_b["rx_unique"],
                "aggregate_rx": agg,
            }
        else:
            delivery = {"skipped": True}

        rep = {
            "repo": "temperature-sensor-prepared",
            "branch": branch,
            "sha": sha,
            "start_branch": START_BRANCH,
            "start_sha": START_SHA,
            "destination": f"{udp_host}:{UDP_PORT}",
            "config": {
                "wifi_cache": 1,
                "period_s": PERIOD_S,
                "attempts": MEASURED,
                "pre_settle_ms": PRE_SETTLE_MS,
                "post_hold_ms": POST_HOLD_MS,
                "voltage_mV": VOLTAGE_MV,
            },
            "ppk": {
                "continuous_capture": True,
                "raw_sample_integration": True,
                "segmentation_depends_on_udp": False,
                "warmup_windows": 1,
                "measured_windows_detected": measured_detected,
                "valid_windows": valid_n,
                "invalid_windows": len(rows) - valid_n,
            },
            "PPK_RUN": ppk_run,
            "udp": udp_sum,
            "send_time": agg_stats(times),
            "send_charge": agg_stats(charges),
            "send_energy": agg_stats(energies),
            "interval_from_ppk": {
                "start_to_start": agg_stats(sts),
                "sleep_duration": agg_stats(sleeps),
            },
            "rx_success": {
                "n": len(rx_ok),
                "median_ms": statistics.median([r["duration_ms"] for r in rx_ok]) if rx_ok else None,
                "median_mC": statistics.median([r["charge_mC"] for r in rx_ok]) if rx_ok else None,
                "p95_mC": pct([r["charge_mC"] for r in rx_ok], 95) if rx_ok else None,
            },
            "rx_lost": {
                "n": len(rx_lost),
                "median_ms": statistics.median([r["duration_ms"] for r in rx_lost]) if rx_lost else None,
                "median_mC": statistics.median([r["charge_mC"] for r in rx_lost]) if rx_lost else None,
                "p95_mC": pct([r["charge_mC"] for r in rx_lost], 95) if rx_lost else None,
            },
            "delivery_confirmation": delivery,
            "artifacts": {
                "csv": str(CSV_OUT),
                "json": str(JSON_OUT),
                "report": str(MD_OUT),
                "raw_capture_local": str(final_bin),
                "raw_windows_local": str(WINDOWS_DIR),
            },
            "BUILD": "PASS",
            "FLASH": "PASS",
            "RUN": "PASS" if ppk_run == "PASS" else "FAIL",
            "rows": rows,
        }
        JSON_OUT.write_text(json.dumps({k: v for k, v in rep.items() if k != "rows"}, indent=2), encoding="utf-8")
        write_report(rep)
        udp.close()

        print(
            f"FINAL 2S BASELINE: median={rep['send_time'].get('median')} ms, "
            f"{rep['send_charge'].get('median')} mC, {rep['send_energy'].get('median')} mJ; "
            f"PPK windows={valid_n}/100; UDP RX={ppk_rx}/100; "
            f"delivery={delivery.get('aggregate_rx', 'n/a')}/300",
            flush=True,
        )
        return 0 if ppk_run == "PASS" else 3
    finally:
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
