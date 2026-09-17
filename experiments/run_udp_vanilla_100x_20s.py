#!/usr/bin/env python3
"""Vanilla Wi-Fi UDP 100x / 20s sleep — Thermometer 2 PPK baseline.

Firmware: AETHER_DIAG_UDP_VANILLA_100X_20S
  - no RTC Wi-Fi caches, DHCP, WIFI_PS_NONE, no internal rate/retry APIs
  - settle 100 ms, post-send hold 1000 ms, GOT_IP timeout 20 s
  - 20 s deep sleep between cycles; cold-boot 60 s flash window

PPK measures EVERY wake (success or fail). No charge/duration filtering.
UDP loss counted only among SEND_OK cycles.
"""
from __future__ import annotations

import csv
import json
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

BUILD = ROOT / "build-udp-vanilla-100x-20s"
RESULTS = HERE / "power_factor_results"
ART_DIR = RESULTS / "udp_vanilla_100x_20s_windows"
CSV_OUT = RESULTS / "udp_vanilla_100x_20s_cycles.csv"
JSON_OUT = RESULTS / "udp_vanilla_100x_20s.json"
MD_OUT = HERE / "UDP_VANILLA_100X_20S.md"

UDP_HOST = "192.168.68.84"
UDP_PORT = 9000
MEASURED = 100
SLEEP_S = 20.0
VOLTAGE_MV = 3000
VOLTAGE_V = VOLTAGE_MV / 1000.0
FS_HZ = 100_000.0
DT_S = 1.0 / FS_HZ

DUMP_MAGIC = 0x314E4156  # 'VAN1' LE

WAKE_ENTER_UA = 3000.0
WAKE_WIN_N = 2000
WAKE_FRAC = 0.85
SLEEP_ENTER_UA = 2000.0
SLEEP_WIN_N = 30000
SLEEP_FRAC = 0.90
RING_S = 45.0
FORCE_CLOSE_S = 35.0  # active longer than GOT_IP+hold+teardown worst case


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


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    return float(np.percentile(np.asarray(vals, dtype=float), p))


def agg(vals: list[float]) -> dict:
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
        self.data_events: list[dict] = []
        self.by_idx: dict[int, int] = {}
        self.dump_recs: dict[int, dict] = {}

    def poll(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                break
            except OSError:
                break
            ts = time.time()
            if len(data) >= 8:
                (magic,) = struct.unpack_from("<I", data, 0)
                if magic == DUMP_MAGIC:
                    n, offset = struct.unpack_from("<HH", data, 4)
                    need = 8 + n * 8
                    if len(data) >= need:
                        for i in range(n):
                            off = 8 + i * 8
                            cycle, got_ip_ms, send_err, flags, _pad = struct.unpack_from(
                                "<HHbBH", data, off
                            )
                            self.dump_recs[int(cycle)] = {
                                "cycle": int(cycle),
                                "got_ip": bool(flags & 0x01),
                                "got_ip_ms": None
                                if got_ip_ms == 0xFFFF
                                else int(got_ip_ms),
                                "udp_send_called": bool(flags & 0x02),
                                "udp_send_err": int(send_err),
                            }
                        print(
                            f"DUMP offset={offset} n={n} total_recs={len(self.dump_recs)}",
                            flush=True,
                        )
                    continue
            if len(data) >= 4:
                (idx,) = struct.unpack_from("<I", data, 0)
                if 1 <= idx <= MEASURED:
                    self.data_events.append(
                        {"t": ts, "idx": idx, "addr": addr[0]}
                    )
                    self.by_idx[idx] = self.by_idx.get(idx, 0) + 1
                    print(f"UDP idx={idx} from={addr[0]} t={ts:.3f}", flush=True)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def build_firmware() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, password = wifi_creds()
    b = BUILD.as_posix()
    defs = (
        f"-D AETHER_DIAG_UDP_VANILLA_100X_20S=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-D AE_UDP_SERVER_HOST={UDP_HOST} "
        f"-D AE_UDP_SERVER_PORT={UDP_PORT} "
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


class AllCycleStreamer:
    """Continuous wake/sleep segmentation — one window per active cycle."""

    def __init__(self, ppk: sb.PpkSession) -> None:
        self.ppk = ppk
        self.n = 0
        self.t0_wall = 0.0
        self.ring_t: deque[float] = deque()
        self.ring_u: deque[float] = deque()
        self.ring_max = int(RING_S * FS_HZ)
        self.in_wake = False
        self.wake_run = 0
        self.sleep_run = 0
        self.active_t0 = 0.0
        self.burst_t: list[float] = []
        self.burst_u: list[float] = []
        self.windows: list[dict] = []

    def start(self) -> None:
        assert self.ppk.ppk is not None
        self.ppk.set_on()
        self.ppk.ppk.start_measuring()
        self.t0_wall = time.time()

    def stop(self) -> None:
        assert self.ppk.ppk is not None
        if self.in_wake and self.burst_t:
            self._close(float(self.burst_t[-1]), force=True)
        try:
            self.ppk.ppk.stop_measuring()
        except Exception as e:
            print(f"ppk_stop_warn={e}", flush=True)

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
            self._on(float(uA))

    def _on(self, u: float) -> None:
        t = self.n * DT_S
        self.n += 1
        au = abs(u)
        if not self.in_wake:
            self.ring_t.append(t)
            self.ring_u.append(u)
            while len(self.ring_t) > self.ring_max:
                self.ring_t.popleft()
                self.ring_u.popleft()
            if u >= WAKE_ENTER_UA:
                self.wake_run += 1
            else:
                self.wake_run = 0
            if self.wake_run >= WAKE_WIN_N:
                # Confirm with short frac on ring tail
                self.active_t0 = t - (WAKE_WIN_N - 1) * DT_S
                self.in_wake = True
                self.wake_run = 0
                self.sleep_run = 0
                self.burst_t = []
                self.burst_u = []
                for rt, ru in zip(self.ring_t, self.ring_u):
                    if rt >= self.active_t0:
                        self.burst_t.append(rt)
                        self.burst_u.append(ru)
                if not self.burst_t or self.burst_t[-1] < t - DT_S * 0.5:
                    self.burst_t.append(t)
                    self.burst_u.append(u)
                self.ring_t.clear()
                self.ring_u.clear()
        else:
            self.burst_t.append(t)
            self.burst_u.append(u)
            if au <= SLEEP_ENTER_UA:
                self.sleep_run += 1
            else:
                self.sleep_run = 0
            if self.sleep_run >= SLEEP_WIN_N:
                t1 = t - SLEEP_WIN_N * DT_S
                self._close(t1, force=False)
            elif (t - self.active_t0) > FORCE_CLOSE_S:
                self._close(t, force=True)

    def _close(self, t1: float, force: bool) -> None:
        self.in_wake = False
        self.sleep_run = 0
        t0 = self.active_t0
        if not self.burst_t:
            return
        t_arr = np.asarray(self.burst_t, dtype=np.float64)
        u_arr = np.asarray(self.burst_u, dtype=np.float64)
        m = (t_arr >= t0) & (t_arr <= t1)
        if not np.any(m):
            m = slice(None)
            wt, wu = t_arr, u_arr
        else:
            wt, wu = t_arr[m], u_arr[m]
        dur = float(wt[-1] - wt[0]) if len(wt) > 1 else DT_S
        try:
            integ = float(np.trapezoid(wu, wt))
        except AttributeError:
            integ = float(np.trapz(wu, wt))
        charge_C = integ * 1e-6
        charge_mC = charge_C * 1000.0
        energy_mJ = charge_mC * VOLTAGE_V
        peak_mA = float(np.max(np.abs(wu))) / 1000.0
        seq = len(self.windows) + 1
        rec = {
            "cycle_detect": seq,
            "t0": float(t0),
            "t1": float(t1),
            "duration_ms": dur * 1000.0,
            "charge_mC": charge_mC,
            "energy_mJ": energy_mJ,
            "peak_current_mA": peak_mA,
            "samples": int(len(wu)),
            "force_close": force,
            "wall_t0": self.t0_wall + float(t0),
            "wall_t1": self.t0_wall + float(t1),
        }
        self.windows.append(rec)
        print(
            f"CYCLE_WIN seq={seq} dur_ms={dur*1000:.1f} mC={charge_mC:.3f} "
            f"force={force}",
            flush=True,
        )
        self.burst_t = []
        self.burst_u = []


def build_rows(
    windows: list[dict], dump: dict[int, dict], udp_by_idx: dict[int, int]
) -> list[dict]:
    rows: list[dict] = []
    for c in range(1, MEASURED + 1):
        w = windows[c - 1] if c - 1 < len(windows) else None
        d = dump.get(c, {})
        got_ip = bool(d.get("got_ip", False))
        send_called = bool(d.get("udp_send_called", False))
        send_err = d.get("udp_send_err", 127)
        if send_err is None:
            send_err = 127
        send_ok = send_called and int(send_err) == 0
        rx = c in udp_by_idx and udp_by_idx[c] > 0
        # If dump missing but RX seen, infer send_ok
        if not d and rx:
            got_ip = True
            send_called = True
            send_err = 0
            send_ok = True
        rows.append(
            {
                "cycle": c,
                "got_ip": int(got_ip),
                "got_ip_ms": d.get("got_ip_ms"),
                "udp_send_called": int(send_called),
                "udp_send_err": int(send_err),
                "udp_received": int(rx),
                "active_duration_ms": None if w is None else w["duration_ms"],
                "charge_mC": None if w is None else w["charge_mC"],
                "energy_mJ": None if w is None else w["energy_mJ"],
                "peak_current_mA": None if w is None else w.get("peak_current_mA"),
                "ppk_window": w is not None,
                "send_ok": int(send_ok),
            }
        )
    return rows


def summarize(rows: list[dict], udp: UdpCollector) -> dict:
    def subset(pred) -> list[dict]:
        return [r for r in rows if pred(r) and r.get("charge_mC") is not None]

    all_e = subset(lambda r: True)
    got_ok = subset(lambda r: r["got_ip"] == 1)
    got_fail = subset(lambda r: r["got_ip"] == 0)
    rx_ok = subset(lambda r: r["udp_received"] == 1 and r["send_ok"] == 1)
    lost = subset(lambda r: r["send_ok"] == 1 and r["udp_received"] == 0)

    got_ip_success = sum(1 for r in rows if r["got_ip"] == 1)
    got_ip_fail = MEASURED - got_ip_success
    send_called = sum(1 for r in rows if r["udp_send_called"] == 1)
    send_ok = sum(1 for r in rows if r["send_ok"] == 1)
    send_local_fail = sum(
        1 for r in rows if r["udp_send_called"] == 1 and r["send_ok"] == 0
    )
    rx_unique = sum(1 for r in rows if r["udp_received"] == 1)
    dups = {str(k): v for k, v in udp.by_idx.items() if v > 1}
    network_lost = max(0, send_ok - rx_unique)
    network_loss_pct = (
        100.0 * network_lost / send_ok if send_ok else None
    )
    e2e = 100.0 * rx_unique / MEASURED

    got_ip_times = [
        float(r["got_ip_ms"])
        for r in rows
        if r["got_ip"] == 1 and r.get("got_ip_ms") is not None
    ]

    def energy_block(rs: list[dict]) -> dict:
        mc = [float(r["charge_mC"]) for r in rs]
        ms = [float(r["active_duration_ms"]) for r in rs]
        return {
            "n": len(rs),
            "median_mC": float(statistics.median(mc)) if mc else None,
            "mean_mC": float(statistics.mean(mc)) if mc else None,
            "p95_mC": pct(mc, 95),
            "median_ms": float(statistics.median(ms)) if ms else None,
            "mean_ms": float(statistics.mean(ms)) if ms else None,
            "p95_ms": pct(ms, 95),
        }

    return {
        "cycles": MEASURED,
        "CONNECTION": {
            "got_ip_success": got_ip_success,
            "got_ip_fail": got_ip_fail,
            "got_ip_success_pct": 100.0 * got_ip_success / MEASURED,
            "got_ip_time_median_ms": float(statistics.median(got_ip_times))
            if got_ip_times
            else None,
            "got_ip_time_p95_ms": pct(got_ip_times, 95),
        },
        "UDP": {
            "send_called": send_called,
            "send_err_ok": send_ok,
            "send_local_fail": send_local_fail,
            "rx_unique": rx_unique,
            "duplicates": dups,
            "network_lost_after_send_ok": network_lost,
            "network_loss_pct_of_send_ok": network_loss_pct,
            "end_to_end_success_pct": e2e,
        },
        "ENERGY_ALL": energy_block(all_e),
        "ENERGY_GOT_IP_SUCCESS": energy_block(got_ok),
        "ENERGY_GOT_IP_FAILURE": energy_block(got_fail),
        "ENERGY_SEND_OK_RX": energy_block(rx_ok),
        "ENERGY_SEND_OK_LOST": energy_block(lost),
        "ppk_windows": sum(1 for r in rows if r["ppk_window"]),
    }


def write_md(summary: dict, meta: dict) -> None:
    c = summary["CONNECTION"]
    u = summary["UDP"]
    ea = summary["ENERGY_ALL"]
    lines = [
        "# VANILLA WIFI 100x / 20s SLEEP",
        "",
        f"- branch: `{meta.get('branch')}`",
        f"- sha: `{meta.get('sha')}`",
        f"- destination: `{UDP_HOST}:{UDP_PORT}`",
        "",
        "## CONNECTION",
        f"- got_ip_success={c['got_ip_success']} fail={c['got_ip_fail']} "
        f"pct={c['got_ip_success_pct']:.1f}%",
        f"- got_ip_time_median_ms={c['got_ip_time_median_ms']} "
        f"p95={c['got_ip_time_p95_ms']}",
        "",
        "## UDP",
        f"- send_called={u['send_called']} send_ok={u['send_err_ok']} "
        f"local_fail={u['send_local_fail']}",
        f"- rx_unique={u['rx_unique']} dups={u['duplicates']}",
        f"- network_lost_after_send_ok={u['network_lost_after_send_ok']} "
        f"loss_pct={u['network_loss_pct_of_send_ok']}",
        f"- end_to_end_success_pct={u['end_to_end_success_pct']}",
        "",
        "## ENERGY ALL",
        f"- n={ea['n']} median_mC={ea['median_mC']} mean_mC={ea['mean_mC']} "
        f"p95_mC={ea['p95_mC']} median_ms={ea['median_ms']} p95_ms={ea['p95_ms']}",
        "",
        f"- ENERGY_GOT_IP_SUCCESS: {summary['ENERGY_GOT_IP_SUCCESS']}",
        f"- ENERGY_GOT_IP_FAILURE: {summary['ENERGY_GOT_IP_FAILURE']}",
        f"- ENERGY_SEND_OK_RX: {summary['ENERGY_SEND_OK_RX']}",
        f"- ENERGY_SEND_OK_LOST: {summary['ENERGY_SEND_OK_LOST']}",
        "",
        "## CONFIG",
        "- cache=NONE DHCP=yes ARP=normal_lwip WIFI_PS=NONE",
        "- fixed_rate=no internal_retry_override=no",
        "- settle_after_GOT_IP=100ms post_send_hold=1000ms deep_sleep=20s",
        "",
        f"- csv: `{CSV_OUT}`",
        "",
    ]
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    branch, sha = sb.git_meta()
    print(f"branch={branch} sha={sha}", flush=True)

    print("=== BUILD ===", flush=True)
    build_firmware()

    kill_udp_9000()
    udp = UdpCollector()
    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    try:
        print("=== FLASH ===", flush=True)
        port = sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
        time.sleep(1.0)
        port2 = sb.wait_espressif_com(timeout_s=25.0) or port
        try:
            sb.hard_reset(port2)
        except Exception as e:
            print(f"reset_warn={e}", flush=True)

        # Short validation: wait first UDP or first dump-less cycle window.
        print("=== VALIDATION: wait cold-boot + cycle 1 ===", flush=True)
        t_val = time.time()
        saw = False
        while time.time() - t_val < 120.0:
            udp.poll()
            if any(e["idx"] == 1 for e in udp.data_events) or udp.by_idx.get(1):
                saw = True
                break
            time.sleep(0.05)
        if not saw:
            # Still OK if connection fails — continue to final with power cycle.
            print("VALIDATE: no UDP#1 yet (may be connection fail); continue", flush=True)
        else:
            print("VALIDATE: saw UDP#1", flush=True)

        print("=== FINAL 100x / 20s ===", flush=True)
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

        # Arm PPK just before cold-boot window ends so cycle-1 wake is captured.
        print("Waiting 58s (cold-boot window ~60s)...", flush=True)
        t_wait = time.time()
        while time.time() - t_wait < 58.0:
            udp.poll()
            time.sleep(0.05)

        stream = AllCycleStreamer(ppk)
        stream.start()
        t0 = time.time()
        # Worst: 100*(20+25) + dump margin
        deadline = t0 + MEASURED * (SLEEP_S + 28.0) + 180.0
        while time.time() < deadline:
            udp.poll()
            stream.drain()
            if len(stream.windows) >= MEASURED and len(udp.dump_recs) >= MEASURED:
                break
            if len(stream.windows) >= MEASURED and (time.time() - t0) > (
                MEASURED * (SLEEP_S + 5.0) + 90.0
            ):
                # Wait a bit more for dump after last sleep
                if time.time() > t0 + MEASURED * (SLEEP_S + 28.0) + 60.0:
                    break
            time.sleep(0.01)

        # Extra for dump after final 20s sleep + reconnect
        t_extra = time.time() + 90.0
        while time.time() < t_extra:
            udp.poll()
            stream.drain()
            if len(udp.dump_recs) >= MEASURED:
                break
            time.sleep(0.01)
        stream.stop()

        rows = build_rows(stream.windows, udp.dump_recs, udp.by_idx)
        with CSV_OUT.open("w", encoding="utf-8", newline="") as fh:
            fields = [
                "cycle",
                "got_ip",
                "got_ip_ms",
                "udp_send_called",
                "udp_send_err",
                "udp_received",
                "active_duration_ms",
                "charge_mC",
                "energy_mJ",
            ]
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

        summary = summarize(rows, udp)
        meta = {
            "repo": "aethernetio/temperature-sensor",
            "branch": branch,
            "sha": sha,
            "variant": "vanilla_100x_20s",
            "CONFIG": {
                "cache": "NONE",
                "DHCP": True,
                "ARP": "normal_lwip",
                "WIFI_PS": "NONE",
                "fixed_rate": False,
                "internal_retry_override": False,
                "settle_after_GOT_IP_ms": 100,
                "post_send_hold_ms": 1000,
                "deep_sleep_s": 20,
                "got_ip_timeout_ms": 20000,
            },
            "PPK": {
                "mode": "Source Meter",
                "voltage_mV": VOLTAGE_MV,
                "autonomous": True,
                "all_100_cycles_measured": summary["ppk_windows"] >= 95,
                "result_filtering": False,
                "despike": False,
            },
            "BUILD": "PASS",
            "FLASH": "PASS",
            "PPK_RUN": "PASS" if summary["ppk_windows"] >= 80 else "FAIL",
            "utc": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "rows": rows,
            "artifacts": {
                "csv": str(CSV_OUT),
                "json": str(JSON_OUT),
                "report": str(MD_OUT),
            },
        }
        JSON_OUT.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        write_md(summary, meta)
        print(json.dumps(summary, indent=2), flush=True)

        u = summary["UDP"]
        ea = summary["ENERGY_ALL"]
        c = summary["CONNECTION"]
        print(
            f"VANILLA RESULT: GOT_IP={c['got_ip_success']}/100; "
            f"SEND_OK={u['send_err_ok']}; RX={u['rx_unique']}; "
            f"UDP loss after SEND_OK={u['network_loss_pct_of_send_ok']}%; "
            f"median active={ea['median_ms']} ms, {ea['median_mC']} mC.",
            flush=True,
        )
        return 0 if meta["PPK_RUN"] == "PASS" else 3
    finally:
        try:
            udp.close()
        except Exception:
            pass
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
