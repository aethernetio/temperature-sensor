#!/usr/bin/env python3
"""Æther prepared HOT 100x / 2 s / PPK full capture + C++ TCP receiver E2E."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402
from ppk_stream_cycle_segmenter import CycleWindow, SegmenterConfig, StreamingCycleSegmenter  # noqa: E402
from run_udp_100x_2s_ppk_fullcapture import (  # noqa: E402
    COLD_BOOT_S,
    FS_HZ,
    GUARD_SAMPLES,
    MEASURED,
    PERIOD_S,
    VOLTAGE_MV,
    VOLTAGE_V,
    agg_stats,
    build_rows,
    integrate_window,
    pct,
    resegment_offline,
)

BUILD = ROOT / "build-aether-hot-100x-2s-ppk"
RX_BUILD = ROOT / "temperature_receiver" / "build-aether-hot-tcp-rx"
RX_EXE = RX_BUILD / "aether_hot_tcp_receiver.exe"
RESULTS = HERE / "power_factor_results"
LOCAL_RAW = RESULTS / "aether_hot_100x_2s_ppk_tcp_rx_raw"
WINDOWS_DIR = LOCAL_RAW / "windows"
CSV_OUT = RESULTS / "aether_hot_100x_2s_ppk_tcp_rx_sends.csv"
JSON_OUT = RESULTS / "aether_hot_100x_2s_ppk_tcp_rx.json"
MD_OUT = HERE / "AETHER_HOT_100X_2S_PPK_TCP_RX.md"
RX_STATE = HERE / "aether_hot_tcp_rx_session"

START_BRANCH = ""
START_SHA = ""
RE_RX_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_HOT_RX = re.compile(
    r"AETHER_HOT_RX wall_ms=\d+ run_id=(\d+) seq=(\d+) duplicate=(\d+) out_of_order=(\d+)"
)
RE_FULL_READY = re.compile(r"AETHER_FULL_READY run_id=(\d+)")
RE_TCP_OK = re.compile(r"RX_TCP_VERIFIED=yes")
RE_FAIL_TCP = re.compile(r"FAIL_TCP_RECEIVER=")


def wifi_creds() -> tuple[str, str]:
    return "chirkov", "kcdjepWz51"


def git_meta() -> tuple[str, str]:
    b = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, cwd=ROOT, check=False)
    s = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=False)
    return (b.stdout or "").strip(), (s.stdout or "").strip()


def build_receiver() -> None:
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    user_cfg = (ROOT / "temperature_receiver" / "user_config.h").as_posix()
    cmd = [
        "cmake",
        "-S",
        str(ROOT / "temperature_receiver"),
        "-B",
        str(RX_BUILD),
        "-G",
        "Ninja",
        f"-DUSER_CONFIG={user_cfg}",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"receiver cmake failed:\n{r.stdout}\n{r.stderr}")
    r = subprocess.run(["ninja", "-C", str(RX_BUILD), "aether_hot_tcp_receiver"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"receiver build failed:\n{r.stdout}\n{r.stderr}")
    if not RX_EXE.is_file():
        raise RuntimeError(f"missing {RX_EXE}")


def build_firmware(service_uid: str, *, hot_attempts: int = MEASURED, period_ms: int = 2000) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, password = wifi_creds()
    b = BUILD.as_posix()
    defs = (
        f"-D AETHER_DIAG_AETHER_HOT_100X_2S_PPK=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-D SERVICE_UID={service_uid} "
        f"-D AE_POWER_BENCH_VARIANT=400 "
        f"-D AE_POWER_BENCH_ARM_MS=0 "
        f"-D AETHER_POWER_BENCH_HOT_ATTEMPTS={hot_attempts} "
        f"-D AETHER_POWER_BENCH_HOT_SLEEP_MS={period_ms} "
        f"-D AETHER_PREPARED_HOT_RESERVE=128 "
        f"-D BENCH_CLIENT_ID=aether_hot_tcp_bench_v1 "
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
        raise RuntimeError(f"esp build failed code={r.returncode}")


class TcpReceiverProc:
    def __init__(self, state_dir: Path, tsv_path: Path, expected: int) -> None:
        self.state_dir = state_dir
        self.tsv_path = tsv_path
        self.expected = expected
        self.proc: subprocess.Popen[str] | None = None
        self.lines: deque[str] = deque(maxlen=50_000)
        self.lock = threading.Lock()
        self.receiver_uid = ""
        self.tcp_verified = False
        self.fail_tcp = False
        self.events: list[dict] = []
        self.full_ready_runs: set[int] = set()

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            with self.lock:
                self.lines.append(line)
            print(f"RX|{line}", flush=True)
            m = RE_RX_UID.search(line)
            if m:
                self.receiver_uid = m.group(1)
            if RE_TCP_OK.search(line):
                self.tcp_verified = True
            if RE_FAIL_TCP.search(line):
                self.fail_tcp = True
            m = RE_FULL_READY.search(line)
            if m:
                self.full_ready_runs.add(int(m.group(1)))
            m = RE_HOT_RX.search(line)
            if m:
                run_id, seq, dup, ooo = map(int, m.groups())
                self.events.append(
                    {
                        "t": time.time(),
                        "run_id": run_id,
                        "seq": seq,
                        "duplicate": dup,
                        "out_of_order": ooo,
                    }
                )

    def start(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["AE_RECEIVER_SESSION_DIR"] = str(self.state_dir.resolve())
        cmd = [
            str(RX_EXE),
            "--state-dir",
            str(self.state_dir.resolve()),
            "--require-tcp",
            "--expected",
            str(self.expected),
            "--tsv",
            str(self.tsv_path.resolve()),
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.state_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def wait_ready(self, timeout_s: float = 120.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.fail_tcp:
                raise RuntimeError("FAIL_TCP_RECEIVER during startup")
            if self.receiver_uid and self.tcp_verified:
                return
            time.sleep(0.2)
        raise RuntimeError("receiver TCP / UID not ready in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def rx_map(self, run_id: int) -> dict[int, dict]:
        by: dict[int, list] = {}
        for e in self.events:
            if e["run_id"] != run_id:
                continue
            seq = e["seq"]
            if seq < 1 or seq > self.expected:
                continue
            by.setdefault(seq, []).append(e)
        out: dict[int, dict] = {}
        for seq, evs in by.items():
            first = evs[0]
            out[seq] = {
                "received": True,
                "duplicate_count": max(0, len(evs) - 1),
                "t_wall": first["t"],
                "out_of_order": any(x["out_of_order"] for x in evs),
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
        self.cfg = SegmenterConfig(fs_hz=FS_HZ, cold_boot_ignore_s=COLD_BOOT_S, max_measured=MEASURED)
        self.segmenter = StreamingCycleSegmenter(self.cfg)

    def start(self) -> None:
        self.wall_start = time.time()
        self.ppk.set_on()
        self.ppk.ppk.start_measuring()

    def stop(self) -> None:
        self.segmenter.finalize()
        self.ppk.ppk.stop_measuring()
        self.bin_f.close()
        meta = {
            "fs_hz": FS_HZ,
            "wall_start_utc": datetime.fromtimestamp(self.wall_start, tz=timezone.utc).isoformat(),
            "sample_count": self.sample_count,
            "voltage_mV": VOLTAGE_MV,
            "raw_bin": str(self.raw_bin),
        }
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def drain(self) -> None:
        raw = self.ppk.ppk.get_data()
        if not raw:
            return
        chunk, _ = self.ppk.ppk.get_samples(raw)
        if not chunk:
            return
        arr = np.asarray(chunk, dtype=np.float32)
        arr.tofile(self.bin_f)
        self.sample_count += len(arr)
        self.segmenter.feed_chunk(arr)


def run_ppk_capture(ppk: sb.PpkSession, label: str, raw_bin: Path) -> StreamingCycleSegmenter:
    meta = raw_bin.with_suffix(".json")
    cap = ContinuousPpkCapture(ppk, raw_bin, meta)
    cap.start()
    t0 = time.time()
    deadline = t0 + COLD_BOOT_S + MEASURED * 6 + 180
    while time.time() < deadline:
        cap.drain()
        if cap.segmenter.measured_windows >= MEASURED:
            time.sleep(2.0)
            cap.drain()
            break
        time.sleep(0.002)
    cap.stop()
    print(
        f"CAPTURE {label} measured={cap.segmenter.measured_windows} samples={cap.sample_count}",
        flush=True,
    )
    return cap.segmenter


def build_aether_rows(windows: list[CycleWindow], raw_bin: Path, rx: dict[int, dict]) -> list[dict]:
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
        rxinfo = rx.get(seq_i, {"received": False, "duplicate_count": 0, "out_of_order": False})
        sts = w.wake_start_s - prev_wake if prev_wake is not None else None
        sleep_d = w.wake_start_s - prev_end if prev_end is not None else None
        rows.append(
            {
                **base,
                "start_to_start_s": sts,
                "sleep_duration_s": sleep_d,
                "receiver_received": bool(rxinfo.get("received")),
                "duplicate_count": int(rxinfo.get("duplicate_count", 0)),
                "out_of_order": bool(rxinfo.get("out_of_order")),
            }
        )
        prev_wake = w.wake_start_s
        prev_end = w.wake_end_s
    return rows


def write_csv(rows: list[dict], run_id: int) -> None:
    fields = [
        "run_id",
        "sequence",
        "duration_ms",
        "charge_mC",
        "energy_mJ",
        "mean_current_mA",
        "peak_current_mA",
        "start_to_start_s",
        "sleep_duration_s",
        "receiver_received",
        "duplicate_count",
        "out_of_order",
        "valid",
        "invalid_reason",
        "raw_window_path",
    ]
    write_header = not CSV_OUT.exists() or CSV_OUT.stat().st_size == 0
    with CSV_OUT.open("a", encoding="utf-8", newline="") as f:
        if write_header:
            f.write(",".join(fields) + "\n")
        for r in rows:
            f.write(
                ",".join(
                    str(r.get(k, "")) if k != "run_id" else str(run_id)
                    for k in fields
                )
                + "\n"
            )


def main() -> int:
    global START_BRANCH, START_SHA
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--smoke-only", action="store_true")
    ap.add_argument("--skip-ppk", action="store_true")
    args = ap.parse_args()

    START_BRANCH, START_SHA = git_meta()
    hot_n = 5 if args.smoke_only else MEASURED
    print(f"START_BRANCH={START_BRANCH} START_SHA={START_SHA}", flush=True)

    RESULTS.mkdir(parents=True, exist_ok=True)
    LOCAL_RAW.mkdir(parents=True, exist_ok=True)
    WINDOWS_DIR.mkdir(parents=True, exist_ok=True)

    if not args.skip_build or not RX_EXE.is_file():
        print("=== BUILD RECEIVER ===", flush=True)
        build_receiver()

    print("=== START RECEIVER (TCP) ===", flush=True)
    rx = TcpReceiverProc(RX_STATE, RESULTS / "aether_hot_tcp_receiver_run1.tsv", hot_n)
    rx.start()
    try:
        rx.wait_ready()
    except RuntimeError:
        rx.stop()
        return 2
    print(f"RECEIVER_UID={rx.receiver_uid}", flush=True)

    if not args.skip_build or not (BUILD / "flash_args").exists():
        print("=== BUILD ESP ===", flush=True)
        build_firmware(rx.receiver_uid, hot_attempts=hot_n)

    if args.skip_ppk:
        rx.stop()
        return 0

    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    try:
        print("=== FLASH ===", flush=True)
        sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
        time.sleep(1.0)
        port = sb.wait_espressif_com(timeout_s=25.0)
        if port:
            sb.hard_reset(port)
        ppk.power_cycle()
        port = sb.wait_espressif_com(timeout_s=50.0)
        if port:
            sb.hard_reset(port)

        raw_bin = LOCAL_RAW / ("smoke.bin" if args.smoke_only else "run1.bin")
        seg = run_ppk_capture(ppk, "PPK_RUN1", raw_bin)
        measured, windows = resegment_offline(raw_bin)
        run_ids = sorted({e["run_id"] for e in rx.events}) or [0]
        run_id = run_ids[-1]
        rx_map = rx.rx_map(run_id)
        rows = build_aether_rows(windows, raw_bin, rx_map)
        write_csv(rows, run_id)

        d_ms = [r["duration_ms"] for r in rows if r.get("valid")]
        d_mc = [r["charge_mC"] for r in rows if r.get("valid")]
        rep = {
            "branch": START_BRANCH,
            "sha": START_SHA,
            "receiver_uid": rx.receiver_uid,
            "tcp_verified": rx.tcp_verified,
            "measured_windows_detected": measured,
            "valid_windows": sum(1 for r in rows if r.get("valid")),
            "hot_rx": sum(1 for r in rows if r.get("receiver_received")),
            "full_ready_runs": sorted(rx.full_ready_runs),
            "run_id": run_id,
            "stats": {
                "duration_ms": agg_stats(d_ms),
                "charge_mC": agg_stats(d_mc),
            },
            "raw_ppk_local": str(LOCAL_RAW),
        }
        JSON_OUT.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        MD_OUT.write_text(
            f"# Æther HOT 100x / 2s / PPK / TCP receiver\n\n"
            f"- receiver_uid: `{rx.receiver_uid}`\n"
            f"- TCP verified: {rx.tcp_verified}\n"
            f"- PPK measured windows: {measured}\n"
            f"- E2E HOT RX: {rep['hot_rx']}/{hot_n}\n"
            f"- json: `{JSON_OUT}`\n",
            encoding="utf-8",
        )
        print(json.dumps(rep, indent=2), flush=True)
        rx.stop()
        return 0 if measured >= min(hot_n, 95) and rx.tcp_verified else 1
    finally:
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
