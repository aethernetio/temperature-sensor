#!/usr/bin/env python3
"""UDP 100x / 1 min / PPK baseline: measure current firmware SEND cost only.

Cold-boot 60 s flash window is in firmware. This runner does not change Wi-Fi
caching, retry, TX power, payload, teardown, or GPIO shutdown.

Streaming PPK: threshold finds active-burst boundaries only; integrate every
sample inside each window (no ua filter, no despike). Sleep energy is not
reported. Giant full-run RAW is not kept — only per-send window artifacts.
"""
from __future__ import annotations

import csv
import json
import math
import os
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

BUILD = ROOT / "build-udp-100x-1min-ppk"
RESULTS = HERE / "power_factor_results"
ART_DIR = RESULTS / "udp_100x_1min_ppk_windows"
CSV_OUT = RESULTS / "udp_100x_1min_ppk_sends.csv"
JSON_OUT = RESULTS / "udp_100x_1min_ppk_baseline.json"
MD_OUT = HERE / "UDP_100X_1MIN_PPK_BASELINE.md"

UDP_HOST = "192.168.68.84"
UDP_PORT = 9000
PRE_SETTLE_MS = 50
POST_HOLD_MS = 200
MEASURED = 100
PERIOD_S = 60.0
VOLTAGE_MV = 3000
VOLTAGE_V = VOLTAGE_MV / 1000.0
FS_HZ = 100_000.0
DT_S = 1.0 / FS_HZ

# Boundary detector only (not an integration filter). Spike-tolerant via
# rolling fraction so PPK autorange phantoms do not glue adjacent cycles.
WAKE_ENTER_UA = 3000.0
WAKE_WIN_N = 2000  # 20 ms
WAKE_FRAC = 0.85
SLEEP_ENTER_UA = 2000.0
SLEEP_WIN_N = 30000  # 300 ms
SLEEP_FRAC = 0.90
PRE_GUARD_S = 1.5
POST_GUARD_S = 1.5
RING_S = 25.0
SAVE_WINDOW_RAW = True
CROSS_CHECK_REL_ERR = 0.35
# After UDP RX, wait for teardown+sleep before segmenting that send.
UDP_SEGMENT_DELAY_S = 3.5
MAX_ACTIVE_S = 12.0
MIN_ACTIVE_S = 0.15


def wifi_creds() -> tuple[str, str]:
    return "chirkov", "kcdjepWz51"


def kill_udp_9000() -> None:
    if sys.platform != "win32":
        return
    r = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, check=False
    )
    pids: set[str] = set()
    for line in (r.stdout or "").splitlines():
        if ":9000" in line and "UDP" in line.upper():
            parts = line.split()
            if parts:
                pids.add(parts[-1])
    for pid in pids:
        if pid.isdigit() and int(pid) not in (0, os.getpid()):
            subprocess.run(
                ["taskkill", "/PID", pid, "/F"], capture_output=True, check=False
            )


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    return float(np.percentile(np.asarray(vals, dtype=float), p))


def agg_stats(vals: list[float]) -> dict:
    if not vals:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "min": None,
            "max": None,
            "stddev": None,
        }
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


class UdpCollector:
    def __init__(self) -> None:
        import socket

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", UDP_PORT))
        self.sock.setblocking(False)
        self.events: list[dict] = []
        self.by_idx: dict[int, int] = {}

    def poll(self) -> list[int]:
        new: list[int] = []
        while True:
            try:
                data, addr = self.sock.recvfrom(64)
            except BlockingIOError:
                break
            except OSError:
                break
            ts = time.time()
            if len(data) < 4:
                continue
            (idx,) = struct.unpack_from("<I", data, 0)
            self.events.append({"t": ts, "idx": idx, "addr": addr[0]})
            self.by_idx[idx] = self.by_idx.get(idx, 0) + 1
            new.append(idx)
            print(f"UDP idx={idx} from={addr[0]} t={ts:.3f}", flush=True)
        return new

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def summary(self) -> dict:
        measured = [e["idx"] for e in self.events if 1 <= e["idx"] <= MEASURED]
        unique = sorted(set(measured))
        missing = [i for i in range(1, MEASURED + 1) if i not in unique]
        dups = {str(i): n for i, n in self.by_idx.items() if n > 1}
        t_idx = {}
        for e in self.events:
            if 1 <= e["idx"] <= MEASURED and str(e["idx"]) not in t_idx:
                t_idx[str(e["idx"])] = e["t"]
        return {
            "attempts": MEASURED,
            "rx_unique": len(unique),
            "rx_total": len(measured),
            "missing": missing,
            "duplicates": dups,
            "all_idx": [e["idx"] for e in self.events],
            "t_idx": t_idx,
        }


def build_firmware() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, password = wifi_creds()
    b = BUILD.as_posix()
    defs = (
        f"-D AETHER_DIAG_UDP_LOW_POWER_1MIN_10=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-D AE_UDP_SERVER_HOST={UDP_HOST} "
        f"-D AE_UDP_SERVER_PORT={UDP_PORT} "
        f"-D AE_UDP_PRE_SETTLE_MS={PRE_SETTLE_MS} "
        f"-D AE_UDP_POST_SEND_HOLD_MS={POST_HOLD_MS} "
        f"-D AE_UDP_MEASURED_SENDS={MEASURED} "
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


class SendBurstStreamer:
    """Stream PPK into a rolling ring; segment each send after its UDP RX.

    Threshold/fraction logic is only for wake/sleep boundaries. Every raw
    sample between start and end is integrated (no ua filter, no despike).
    """

    def __init__(self, ppk: sb.PpkSession, art_dir: Path) -> None:
        self.ppk = ppk
        self.art_dir = art_dir
        self.n = 0
        self.t0_wall = 0.0
        self.ring_t: deque[float] = deque()
        self.ring_u: deque[float] = deque()
        self.ring_max = int(RING_S * FS_HZ)
        self.pending_udp: list[tuple[int, float]] = []  # (idx, t_rel)
        self.done_idx: set[int] = set()
        self.windows: list[dict] = []
        self.wake_edges: list[float] = []
        self.sleep_edges: list[float] = []

    def start(self) -> None:
        assert self.ppk.ppk is not None
        self.art_dir.mkdir(parents=True, exist_ok=True)
        self.ppk.set_on()
        self.ppk.ppk.start_measuring()
        self.t0_wall = time.time()

    def stop(self) -> None:
        assert self.ppk.ppk is not None
        # Final pass on any pending UDP that now has enough post-sleep samples.
        self._try_segment_pending(force=True)
        try:
            self.ppk.ppk.stop_measuring()
        except Exception as e:
            print(f"ppk_stop_warn={e}", flush=True)

    def note_udp(self, idx: int, wall_t: float | None = None) -> None:
        if idx < 1 or idx > MEASURED or idx in self.done_idx:
            return
        t_rel = (wall_t if wall_t is not None else time.time()) - self.t0_wall
        if any(i == idx for i, _ in self.pending_udp):
            return
        self.pending_udp.append((idx, t_rel))
        print(f"PPK_ARM_UDP idx={idx} t_rel={t_rel:.3f}", flush=True)

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
        for uA in chunk:
            t = self.n * DT_S
            self.n += 1
            self.ring_t.append(t)
            self.ring_u.append(float(uA))
            while len(self.ring_t) > self.ring_max:
                self.ring_t.popleft()
                self.ring_u.popleft()
        self._try_segment_pending(force=False)

    def _try_segment_pending(self, force: bool) -> None:
        if not self.ring_t:
            return
        now_t = self.ring_t[-1]
        still: list[tuple[int, float]] = []
        for idx, t_udp in self.pending_udp:
            if (not force) and (now_t - t_udp) < UDP_SEGMENT_DELAY_S:
                still.append((idx, t_udp))
                continue
            rec = self._segment_one(idx, t_udp)
            if rec is None:
                if force or (now_t - t_udp) > 20.0:
                    self.windows.append(
                        {
                            "valid": False,
                            "reason": "segment_failed",
                            "sequence_detect": idx,
                            "t0": None,
                            "t1": None,
                            "duration_ms": None,
                            "charge_mC": None,
                            "energy_mJ": None,
                            "peak_current_mA": None,
                            "wall_t0": self.t0_wall + t_udp,
                            "wall_t1": self.t0_wall + t_udp,
                            "raw_window": None,
                        }
                    )
                    self.done_idx.add(idx)
                    print(f"SEND_WIN seq={idx} valid=False reason=segment_failed", flush=True)
                else:
                    still.append((idx, t_udp))
            else:
                self.windows.append(rec)
                self.done_idx.add(idx)
                # Checkpoint so a killed run still leaves partial CSV.
                try:
                    rows = [
                        {
                            "sequence": w.get("sequence_detect"),
                            "duration_ms": w.get("duration_ms"),
                            "charge_mC": w.get("charge_mC"),
                            "energy_mJ": w.get("energy_mJ"),
                            "peak_current_mA": w.get("peak_current_mA"),
                            "udp_received": True,
                            "valid": bool(w.get("valid")),
                        }
                        for w in self.windows
                    ]
                    write_csv(rows, RESULTS / "udp_100x_1min_ppk_sends_partial.csv")
                except Exception as e:
                    print(f"checkpoint_warn={e}", flush=True)
        self.pending_udp = still

    def _arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return (
            np.asarray(self.ring_t, dtype=np.float64),
            np.asarray(self.ring_u, dtype=np.float64),
        )

    def _find_wake_before(self, t_arr: np.ndarray, u_arr: np.ndarray, t_udp: float) -> float | None:
        # Search [t_udp-MAX_ACTIVE, t_udp] for the LAST sleep->wake before UDP.
        t_lo = t_udp - MAX_ACTIVE_S
        m = (t_arr >= t_lo) & (t_arr <= t_udp)
        if np.count_nonzero(m) < WAKE_WIN_N:
            return None
        idx = np.flatnonzero(m)
        u = np.abs(u_arr[idx])
        step = max(1, int(FS_HZ / 1000))
        u_d = u[::step]
        t_d = t_arr[idx][::step]
        win = max(3, WAKE_WIN_N // step)
        if len(u_d) < win:
            return None
        high = (u_d >= WAKE_ENTER_UA).astype(np.float64)
        c = np.cumsum(high)
        last: float | None = None
        for i in range(win - 1, len(u_d)):
            if i < win:
                s = c[i]
                denom = i + 1
            else:
                s = c[i] - c[i - win]
                denom = win
            if (s / denom) < WAKE_FRAC:
                continue
            # Prefer transitions out of a sleep-ish preceding window.
            if i >= win:
                if i >= 2 * win:
                    prev = c[i - win] - c[i - 2 * win]
                    prev_n = win
                else:
                    prev = c[i - win]
                    prev_n = i - win + 1
                if prev_n > 0 and (prev / prev_n) > 0.40:
                    continue
            last = float(t_d[i - win + 1])
        if last is not None:
            return last
        # Fallback: last sample above threshold before UDP.
        for i in range(len(u_d) - 1, -1, -1):
            if u_d[i] >= WAKE_ENTER_UA:
                return float(t_d[i])
        return None

    def _find_sleep_after(self, t_arr: np.ndarray, u_arr: np.ndarray, t_udp: float) -> float | None:
        t_hi = t_udp + MAX_ACTIVE_S
        m = (t_arr >= t_udp) & (t_arr <= t_hi)
        if np.count_nonzero(m) < SLEEP_WIN_N // 10:
            return None
        idx = np.flatnonzero(m)
        u = np.abs(u_arr[idx])
        step = max(1, int(FS_HZ / 1000))
        u_d = u[::step]
        t_d = t_arr[idx][::step]
        win = max(5, SLEEP_WIN_N // step)
        if len(u_d) < win:
            return None
        low = (u_d <= SLEEP_ENTER_UA).astype(np.float64)
        c = np.cumsum(low)
        for i in range(win - 1, len(u_d)):
            s = c[i] - (c[i - win] if i >= win else 0.0)
            denom = win if i >= win else (i + 1)
            if i < win:
                s = c[i]
            if (s / denom) >= SLEEP_FRAC:
                return float(t_d[i - win + 1])
        return None

    def _segment_one(self, idx: int, t_udp: float) -> dict | None:
        t_arr, u_arr = self._arrays()
        if len(t_arr) < 1000:
            return None
        t0 = self._find_wake_before(t_arr, u_arr, t_udp)
        t1 = self._find_sleep_after(t_arr, u_arr, t_udp)
        if t0 is None or t1 is None or t1 <= t0:
            return None
        dur = t1 - t0
        if dur < MIN_ACTIVE_S or dur > MAX_ACTIVE_S:
            # Still save as invalid with numbers if possible
            pass
        m = (t_arr >= t0) & (t_arr <= t1)
        if not np.any(m):
            return None
        wt = t_arr[m]
        wu = u_arr[m]
        try:
            integ_uA_s = float(np.trapezoid(wu, wt))
        except AttributeError:
            integ_uA_s = float(np.trapz(wu, wt))
        charge_C = integ_uA_s * 1e-6
        charge_mC = charge_C * 1000.0
        energy_mJ = charge_mC * VOLTAGE_V
        mean_uA = float(np.mean(wu))
        avg_from_q_uA = (charge_C / max(dur, DT_S)) * 1e6
        rel_err = abs(mean_uA - avg_from_q_uA) / max(abs(mean_uA), 1.0)
        peak_mA = float(np.max(np.abs(wu))) / 1000.0
        valid = True
        reason = ""
        if dur < MIN_ACTIVE_S or dur > MAX_ACTIVE_S:
            valid = False
            reason = f"duration_out_of_range_{dur:.3f}s"
        elif charge_mC < 0.5 or charge_mC > 500.0:
            valid = False
            reason = f"charge_out_of_range_{charge_mC:.3f}mC"
        elif rel_err > 1.0 and abs(mean_uA) > 100.0:
            valid = False
            reason = f"cross_check_rel_err_{rel_err:.3f}"
        art_path = None
        if SAVE_WINDOW_RAW:
            # Include small guards in artifact only.
            g0 = t0 - PRE_GUARD_S
            g1 = t1 + POST_GUARD_S
            gm = (t_arr >= g0) & (t_arr <= g1)
            gt = t_arr[gm]
            gu = u_arr[gm]
            art_path = self.art_dir / f"send_{idx:03d}.csv"
            with art_path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write("t_s,uA\n")
                step = 10
                for i in range(0, len(gt), step):
                    fh.write(f"{gt[i]:.6f},{gu[i]:.4f}\n")
        self.wake_edges.append(t0)
        self.sleep_edges.append(t1)
        rec = {
            "valid": valid,
            "reason": reason,
            "sequence_detect": idx,
            "t0": t0,
            "t1": t1,
            "duration_ms": dur * 1000.0,
            "charge_mC": charge_mC,
            "energy_mJ": energy_mJ,
            "peak_current_mA": peak_mA,
            "mean_uA": mean_uA,
            "avg_from_charge_uA": avg_from_q_uA,
            "rel_err": rel_err,
            "samples": int(len(wu)),
            "wall_t0": self.t0_wall + t0,
            "wall_t1": self.t0_wall + t1,
            "raw_window": str(art_path) if art_path else None,
        }
        print(
            f"SEND_WIN seq={idx} valid={valid} dur_ms={dur*1000:.1f} "
            f"mC={charge_mC:.3f} mJ={energy_mJ:.3f} peak_mA={peak_mA:.1f} "
            f"reason={reason or '-'}",
            flush=True,
        )
        return rec


def pair_windows_to_udp(
    windows: list[dict], udp_sum: dict
) -> list[dict]:
    """Map measured UDP indices 1..N to PPK windows by sequence_detect / UDP."""
    by_seq: dict[int, dict] = {}
    for w in windows:
        seq = int(w.get("sequence_detect") or 0)
        if seq >= 1:
            by_seq[seq] = w
    t_idx = {int(k): float(v) for k, v in (udp_sum.get("t_idx") or {}).items()}
    rows: list[dict] = []
    for seq in range(1, MEASURED + 1):
        udp_rx = seq in t_idx
        w = by_seq.get(seq)
        if w is None:
            rows.append(
                {
                    "sequence": seq,
                    "duration_ms": None,
                    "charge_mC": None,
                    "energy_mJ": None,
                    "peak_current_mA": None,
                    "udp_received": udp_rx,
                    "valid": False,
                    "reason": "missing_ppk_window",
                    "t0": None,
                    "t1": None,
                    "rel_err": None,
                    "raw_window": None,
                    "pair_dt_s": None,
                }
            )
            continue
        rows.append(
            {
                "sequence": seq,
                "duration_ms": w.get("duration_ms"),
                "charge_mC": w.get("charge_mC"),
                "energy_mJ": w.get("energy_mJ"),
                "peak_current_mA": w.get("peak_current_mA"),
                "udp_received": udp_rx,
                "valid": bool(w.get("valid")),
                "reason": w.get("reason") or "",
                "t0": w.get("t0"),
                "t1": w.get("t1"),
                "rel_err": w.get("rel_err"),
                "raw_window": w.get("raw_window"),
                "pair_dt_s": None,
            }
        )
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sequence",
        "duration_ms",
        "charge_mC",
        "energy_mJ",
        "peak_current_mA",
        "udp_received",
        "valid",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "sequence": r["sequence"],
                    "duration_ms": r["duration_ms"],
                    "charge_mC": r["charge_mC"],
                    "energy_mJ": r["energy_mJ"],
                    "peak_current_mA": r["peak_current_mA"],
                    "udp_received": int(bool(r["udp_received"])),
                    "valid": int(bool(r["valid"])),
                }
            )


def start_to_start_mean(udp_sum: dict) -> float | None:
    t_idx = udp_sum.get("t_idx") or {}
    times = []
    for i in range(1, MEASURED + 1):
        if str(i) in t_idx:
            times.append(float(t_idx[str(i)]))
    if len(times) < 2:
        return None
    times.sort()
    dts = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    return float(statistics.mean(dts))


def wait_udp_idx(
    udp: UdpCollector, want: int, timeout_s: float, label: str
) -> bool:
    """Poll until UDP payload index == want (no PPK; avoids cold-boot mis-seg)."""
    print(f"=== WAIT UDP idx={want} ({label}, timeout={timeout_s:.0f}s) ===", flush=True)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        for idx in udp.poll():
            if idx == want:
                print(f"GOT UDP idx={want} after {time.time() - t0:.1f}s", flush=True)
                return True
        time.sleep(0.05)
    print(f"TIMEOUT waiting UDP idx={want}", flush=True)
    return False


def validate_short(ppk: sb.PpkSession, udp: UdpCollector) -> dict:
    """After cold-boot wait + warmup #0, capture measured #1 active window."""
    print("=== VALIDATION: cold-boot skip -> UDP#0 -> measure UDP#1 ===", flush=True)
    if not wait_udp_idx(udp, 0, 120.0, "warmup"):
        return {"ok": False, "reason": "no_udp_warmup_0"}
    # Let #0 teardown finish and enter warmup deep sleep before arming PPK.
    time.sleep(4.0)
    stream = SendBurstStreamer(ppk, ART_DIR / "validation")
    stream.start()
    t0 = time.time()
    saw1 = False
    while time.time() - t0 < 90.0:
        for idx in udp.poll():
            if idx >= 1:
                saw1 = True
                stream.note_udp(idx)
        stream.drain()
        if saw1 and any(
            w.get("sequence_detect") == 1 and w.get("valid") for w in stream.windows
        ):
            break
        time.sleep(0.01)
    t_extra = time.time() + 5.0
    while time.time() < t_extra:
        for idx in udp.poll():
            stream.note_udp(idx)
        stream.drain()
        time.sleep(0.01)
    stream.stop()
    ok = saw1 and any(w.get("valid") for w in stream.windows)
    print(
        f"VALIDATE saw_udp1={saw1} windows={len(stream.windows)} ok={ok}",
        flush=True,
    )
    return {
        "ok": ok,
        "saw_udp_ge1": saw1,
        "windows": len(stream.windows),
        "first_valid": next((w for w in stream.windows if w.get("valid")), None),
    }


def run_final_100(ppk: sb.PpkSession, udp: UdpCollector) -> tuple[SendBurstStreamer, dict]:
    print("=== FINAL 100x60 CAPTURE ===", flush=True)
    if not wait_udp_idx(udp, 0, 150.0, "final warmup"):
        raise RuntimeError("final: no UDP warmup idx=0 after cold boot")
    # Enter warmup sleep so ring contains sleep baseline before measured #1.
    time.sleep(4.0)
    stream = SendBurstStreamer(ppk, ART_DIR / "final")
    stream.start()
    t_start = time.time()
    deadline = t_start + MEASURED * PERIOD_S + 120.0
    got_last = False
    t_last = None
    last_idx = -1
    while time.time() < deadline:
        for idx in udp.poll():
            last_idx = idx
            if idx >= 1:
                stream.note_udp(idx)
            if idx == MEASURED:
                got_last = True
                t_last = time.time()
        stream.drain()
        if got_last and t_last and (time.time() - t_last) > 8.0:
            if len(stream.done_idx) >= MEASURED or (time.time() - t_last) > 30.0:
                break
        time.sleep(0.01)
    t_extra = time.time() + 6.0
    while time.time() < t_extra:
        for idx in udp.poll():
            stream.note_udp(idx)
        stream.drain()
        time.sleep(0.01)
    stream.stop()
    print(
        f"FINAL last_udp={last_idx} windows={len(stream.windows)} "
        f"done={len(stream.done_idx)}",
        flush=True,
    )
    return stream, udp.summary()


def build_report(rows: list[dict], udp_sum: dict, meta: dict) -> dict:
    valid_rows = [r for r in rows if r.get("valid") and r.get("duration_ms") is not None]
    durs = [float(r["duration_ms"]) for r in valid_rows]
    chgs = [float(r["charge_mC"]) for r in valid_rows]
    engs = [float(r["energy_mJ"]) for r in valid_rows]

    def slice_stats(rs: list[dict]) -> dict:
        ds = [float(r["duration_ms"]) for r in rs if r.get("valid") and r.get("duration_ms") is not None]
        cs = [float(r["charge_mC"]) for r in rs if r.get("valid") and r.get("charge_mC") is not None]
        return {
            "mean_time_ms": float(statistics.mean(ds)) if ds else None,
            "median_time_ms": float(statistics.median(ds)) if ds else None,
            "mean_charge_mC": float(statistics.mean(cs)) if cs else None,
            "median_charge_mC": float(statistics.median(cs)) if cs else None,
            "n": len(ds),
        }

    first10 = [r for r in rows if 1 <= r["sequence"] <= 10]
    last10 = [r for r in rows if MEASURED - 9 <= r["sequence"] <= MEASURED]
    sts = start_to_start_mean(udp_sum)
    return {
        **meta,
        "status": "OK",
        "utc": datetime.now(timezone.utc).isoformat(),
        "udp": udp_sum,
        "send_time": agg_stats(durs),
        "send_charge": agg_stats(chgs),
        "send_energy": agg_stats(engs),
        "first_10": slice_stats(first10),
        "last_10": slice_stats(last10),
        "interval": {
            "requested_s": PERIOD_S,
            "actual_start_to_start_mean_s": sts,
            "timer_sleep_s": 60,
        },
        "filtering_of_active_samples": False,
        "despike_used": False,
        "sleep_energy_included": False,
        "rows": rows,
        "artifacts": {
            "csv": str(CSV_OUT),
            "json": str(JSON_OUT),
            "report": str(MD_OUT),
            "raw_windows_location": str(ART_DIR / "final"),
        },
    }


def write_md(rep: dict) -> None:
    st = rep.get("send_time") or {}
    sc = rep.get("send_charge") or {}
    se = rep.get("send_energy") or {}
    udp = rep.get("udp") or {}
    lines = [
        "# UDP 100x / 1 MIN / PPK BASELINE",
        "",
        f"- branch: `{rep.get('branch')}`",
        f"- sha: `{rep.get('sha')}`",
        f"- destination: `{UDP_HOST}:{UDP_PORT}`",
        f"- cold_boot_awake_wait_s: 60 (timer wake skips)",
        f"- firmware_behavior_changed_except_boot_wait: no",
        f"- UDP attempts={udp.get('attempts')} unique={udp.get('rx_unique')} "
        f"missing={udp.get('missing')} dups={udp.get('duplicates')}",
        "",
        "## SEND TIME (ms)",
        f"- valid_count={st.get('n')} mean={st.get('mean')} median={st.get('median')} "
        f"p90={st.get('p90')} p95={st.get('p95')} min={st.get('min')} max={st.get('max')} "
        f"stddev={st.get('stddev')}",
        "",
        "## SEND CHARGE (mC)",
        f"- mean={sc.get('mean')} median={sc.get('median')} p90={sc.get('p90')} "
        f"p95={sc.get('p95')} min={sc.get('min')} max={sc.get('max')} stddev={sc.get('stddev')}",
        "",
        "## SEND ENERGY @ 3.0V (mJ)",
        f"- mean={se.get('mean')} median={se.get('median')} p90={se.get('p90')} "
        f"p95={se.get('p95')} min={se.get('min')} max={se.get('max')}",
        "",
        f"- first10: {rep.get('first_10')}",
        f"- last10: {rep.get('last_10')}",
        f"- interval: {rep.get('interval')}",
        f"- filtering_of_active_samples=no despike=no sleep_energy=no",
        f"- csv: `{CSV_OUT}`",
        f"- windows: `{ART_DIR / 'final'}`",
        "",
    ]
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument(
        "--final-only",
        action="store_true",
        help="Skip flash+validation; power-cycle and run clean final 100",
    )
    args = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    branch, sha = sb.git_meta()
    print(f"branch={branch} sha={sha}", flush=True)

    build_ok = True
    if not args.skip_build and not args.final_only:
        print("=== BUILD ===", flush=True)
        build_firmware()
    else:
        print("=== BUILD skipped ===", flush=True)
        if not (BUILD / "flash_args").exists():
            print("missing build; forcing build", flush=True)
            build_firmware()

    kill_udp_9000()
    udp = UdpCollector()
    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    flash_ok = False
    ppk_ok = False
    val: dict = {"ok": True, "skipped": bool(args.final_only)}
    try:
        if not args.final_only:
            print("=== FLASH ===", flush=True)
            port = sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
            time.sleep(1.0)
            port2 = sb.wait_espressif_com(timeout_s=25.0) or port
            try:
                sb.hard_reset(port2)
            except Exception as e:
                print(f"reset_warn={e}", flush=True)
            flash_ok = True

            val = validate_short(ppk, udp)
            if not val.get("ok"):
                out = {
                    "status": "VALIDATION_FAIL",
                    "validation": val,
                    "branch": branch,
                    "sha": sha,
                    "BUILD": "PASS",
                    "FLASH": "PASS" if flash_ok else "FAIL",
                    "PPK_RUN": "FAIL",
                }
                JSON_OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
                print("VALIDATION FAIL — abort final 100x", flush=True)
                return 2
        else:
            flash_ok = True
            print("=== FINAL-ONLY: skip flash/validation ===", flush=True)

        # Clean final: power-cycle → cold boot 60 s → warmup → 100 measured.
        kill_udp_9000()
        udp.close()
        udp = UdpCollector()
        ppk.power_cycle()
        port3 = sb.wait_espressif_com(timeout_s=50.0)
        if port3:
            try:
                sb.hard_reset(port3)
            except Exception as e:
                print(f"reset_warn={e}", flush=True)

        stream, udp_sum = run_final_100(ppk, udp)
        rows = pair_windows_to_udp(stream.windows, udp_sum)
        write_csv(rows, CSV_OUT)
        meta = {
            "repo": "temperature-sensor-prepared",
            "branch": branch,
            "sha": sha,
            "destination": f"{UDP_HOST}:{UDP_PORT}",
            "cold_boot_awake_wait_s": 60,
            "cold_boot_wait_repeats_after_timer_wake": False,
            "firmware_behavior_changed_except_boot_wait": False,
            "PPK": {
                "mode": "Source Meter",
                "voltage_mV": VOLTAGE_MV,
                "autonomous_power_control": True,
                "autonomous_flash": True,
            },
            "validation": val,
            "BUILD": "PASS" if build_ok else "FAIL",
            "FLASH": "PASS" if flash_ok else "FAIL",
        }
        rep = build_report(rows, udp_sum, meta)
        valid_n = (rep.get("send_time") or {}).get("n") or 0
        ppk_ok = valid_n >= 80
        rep["PPK_RUN"] = "PASS" if ppk_ok else "FAIL"
        JSON_OUT.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        write_md(rep)
        print(json.dumps({k: rep[k] for k in rep if k != "rows"}, indent=2), flush=True)
        return 0 if ppk_ok else 3
    finally:
        try:
            udp.close()
        except Exception:
            pass
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
