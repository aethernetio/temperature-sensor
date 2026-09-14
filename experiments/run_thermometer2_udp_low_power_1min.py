#!/usr/bin/env python3
"""Thermometer 2 combined UDP RTC + low-power 10x60s campaign.

Reuses PPK/flash helpers from run_thermometer2_sleep_bisect.py.
Final measured firmware: silent console, no 25s flash window.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402
import run_prepared_power_factor_study as pf  # noqa: E402

BUILD = ROOT / "build-udp-low-power-1min"
RESULTS = HERE / "power_factor_results"
JSON_OUT = RESULTS / "thermometer2_udp_low_power_1min.json"
MD_OUT = HERE / "THERMOMETER2_UDP_LOW_POWER_1MIN.md"

UDP_HOST = "192.168.68.84"
UDP_PORT = 9000
PRE_SETTLE_MS = 300
POST_HOLDS_MS = (200, 300, 500, 1000)
MEASURED = 10
PERIOD_S = 60.0
VOLTAGE_MV = 3000
CR2_MAH = 800.0
SLEEP_UA_MAX = 200.0
WAKE_UA = 2000.0
LEFTOVER_SLEEP_UA = 500.0
DT_US = 10.0  # PPK2 source-meter sample period


def git_meta() -> tuple[str, str]:
    return sb.git_meta()


def wifi_creds() -> tuple[str, str]:
    w = pf.camp.APS["chirkov"]
    return w["ssid"], w["password"]


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
            print(f"killed_udp_pid={pid}", flush=True)
    time.sleep(0.5)


class UdpCollector:
    def __init__(self) -> None:
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
            if len(data) == 4:
                (idx,) = struct.unpack("<I", data)
            elif len(data) >= 4:
                (idx,) = struct.unpack_from("<I", data, 0)
            else:
                print(f"udp_bad_len={len(data)} from={addr}", flush=True)
                continue
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
        dups = {i: n for i, n in self.by_idx.items() if n > 1}
        warmup = [e for e in self.events if e["idx"] == 0]
        return {
            "warmup_rx": len(warmup),
            "attempts": MEASURED,
            "rx_unique": len(unique),
            "rx_total": len(measured),
            "missing": missing,
            "duplicates": dups,
            "all_idx": [e["idx"] for e in self.events],
            "t_idx": {str(e["idx"]): e["t"] for e in self.events},
        }


class StreamPpk:
    """Online charge/energy + sleep histogram. No full-rate trace."""

    def __init__(self, ppk: sb.PpkSession) -> None:
        self.ppk = ppk
        self.t0 = 0.0
        self.n = 0
        self.charge_uC = 0.0
        self.energy_J = 0.0
        self.sum_uA = 0.0
        self.min_uA = 1e300
        self.max_uA = -1e300
        self.sleep_n = 0
        self.sleep_sum = 0.0
        self.sleep_hist = [0] * 251
        self.wake_edges: list[float] = []
        self.sleep_edges: list[float] = []
        self.in_wake = False
        self.wake_run = 0
        self.sleep_run = 0
        self.last_print = 0.0
        self.decimated: list[tuple[float, float]] = []
        self._decim_i = 0

    def start(self) -> None:
        assert self.ppk.ppk is not None
        self.ppk.set_on()
        self.ppk.ppk.start_measuring()
        self.t0 = time.time()
        self.last_print = self.t0

    def stop(self) -> None:
        assert self.ppk.ppk is not None
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
        v = self.ppk.voltage_mv / 1000.0
        dt_s = DT_US / 1_000_000.0
        for uA in chunk:
            u = float(uA)
            self.n += 1
            self.charge_uC += u * dt_s
            self.energy_J += v * (u / 1_000_000.0) * dt_s
            self.sum_uA += u
            if u < self.min_uA:
                self.min_uA = u
            if u > self.max_uA:
                self.max_uA = u
            au = abs(u)
            if au < SLEEP_UA_MAX:
                self.sleep_n += 1
                self.sleep_sum += u
                bin_i = min(250, max(0, int(au)))
                self.sleep_hist[bin_i] += 1
            t = self.n * dt_s
            if not self.in_wake:
                if u >= WAKE_UA:
                    self.wake_run += 1
                else:
                    self.wake_run = 0
                if self.wake_run >= 200:
                    self.in_wake = True
                    self.wake_run = 0
                    self.sleep_run = 0
                    self.wake_edges.append(t)
            else:
                if au <= SLEEP_UA_MAX:
                    self.sleep_run += 1
                else:
                    self.sleep_run = 0
                if self.sleep_run >= 20000:  # 200 ms @ 100 kHz
                    self.in_wake = False
                    self.sleep_run = 0
                    self.sleep_edges.append(t)
            self._decim_i += 1
            if self._decim_i >= 50000:
                self._decim_i = 0
                if len(self.decimated) < 4000:
                    self.decimated.append((t, u))
        now = time.time()
        if now - self.last_print >= 10.0:
            elapsed = now - self.t0
            avg = (self.sum_uA / self.n) if self.n else 0.0
            savg = (self.sleep_sum / self.sleep_n) if self.sleep_n else float("nan")
            print(
                f"ppk t={elapsed:.1f}s n={self.n} avg_uA={avg:.1f} "
                f"sleep_avg_uA={savg:.2f} sleep_frac="
                f"{(self.sleep_n / self.n) if self.n else 0:.3f} "
                f"wakes={len(self.wake_edges)}",
                flush=True,
            )
            self.last_print = now

    def snapshot(self) -> dict:
        elapsed = time.time() - self.t0 if self.t0 else 0.0
        sleep_avg = (self.sleep_sum / self.sleep_n) if self.sleep_n else float("nan")
        sleep_med = hist_median(self.sleep_hist)
        charge_C = self.charge_uC / 1_000_000.0
        return {
            "elapsed_s": elapsed,
            "samples": self.n,
            "avg_uA": (self.sum_uA / self.n) if self.n else 0.0,
            "min_uA": self.min_uA if self.min_uA < 1e299 else 0.0,
            "max_uA": self.max_uA if self.max_uA > -1e299 else 0.0,
            "sleep_avg_uA": sleep_avg,
            "sleep_median_uA": sleep_med,
            "sleep_frac": (self.sleep_n / self.n) if self.n else 0.0,
            "sleep_samples": self.sleep_n,
            "charge_C": charge_C,
            "energy_J": self.energy_J,
            "wake_edges_s": self.wake_edges[:20],
            "sleep_edges_s": self.sleep_edges[:20],
            "wake_count": len(self.wake_edges),
        }


def hist_median(hist: list[int]) -> float:
    total = sum(hist)
    if total <= 0:
        return float("nan")
    mid = (total + 1) // 2
    acc = 0
    for i, c in enumerate(hist):
        acc += c
        if acc >= mid:
            return float(i)
    return float(len(hist) - 1)


def battery_from_cycle(charge_C: float, sleep_uA: float, voltage_v: float) -> dict:
    q_cycle_C = charge_C / MEASURED if MEASURED else 0.0
    i_avg_a = q_cycle_C / PERIOD_S if PERIOD_S else 0.0
    i_avg_mA = i_avg_a * 1000.0
    life_h = (CR2_MAH / i_avg_mA) if i_avg_mA > 0 else float("inf")
    i_sleep_a = sleep_uA / 1_000_000.0
    q_sleep_C = i_sleep_a * PERIOD_S
    q_wifi_C = max(0.0, q_cycle_C - q_sleep_C)
    e_cycle_J = q_cycle_C * voltage_v
    return {
        "charge_per_cycle_C": q_cycle_C,
        "charge_per_cycle_mC": q_cycle_C * 1000.0,
        "energy_per_cycle_J": e_cycle_J,
        "energy_per_cycle_mJ": e_cycle_J * 1000.0,
        "i_avg_mA": i_avg_mA,
        "life_hours": life_h,
        "life_days": life_h / 24.0,
        "life_months": life_h / 24.0 / 30.44,
        "sleep_charge_C": q_sleep_C,
        "wifi_charge_C": q_wifi_C,
        "sleep_energy_mJ": q_sleep_C * voltage_v * 1000.0,
        "wifi_energy_mJ": q_wifi_C * voltage_v * 1000.0,
    }


def idf_defs(post_ms: int) -> str:
    ssid, password = wifi_creds()
    b = BUILD.as_posix()
    return (
        f"-D AETHER_DIAG_UDP_LOW_POWER_1MIN_10=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-D AE_UDP_SERVER_HOST={UDP_HOST} "
        f"-D AE_UDP_SERVER_PORT={UDP_PORT} "
        f"-D AE_UDP_PRE_SETTLE_MS={PRE_SETTLE_MS} "
        f"-D AE_UDP_POST_SEND_HOLD_MS={post_ms} "
        f"-B '{b}'"
    )


def build_firmware(post_ms: int) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    defs = idf_defs(post_ms)
    extra = f"""
if (-not (Test-Path '{BUILD.as_posix()}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} build
"""
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"build failed post_ms={post_ms} code={r.returncode}")


def analyze_delivery(udp: dict) -> tuple[int, int, float]:
    rx = int(udp.get("rx_unique") or 0)
    attempts = int(udp.get("attempts") or MEASURED)
    loss = 100.0 * (attempts - rx) / attempts if attempts else 100.0
    return attempts, rx, loss


def leftover_power(snap: dict) -> bool:
    sleep_avg = snap.get("sleep_avg_uA")
    frac = snap.get("sleep_frac") or 0.0
    avg = snap.get("avg_uA") or 0.0
    # Milliamp-class floor after teardown: almost no |I|<200µA samples.
    if frac < 0.2 and avg > 800:
        return True
    if (
        isinstance(sleep_avg, (int, float))
        and sleep_avg > LEFTOVER_SLEEP_UA
        and frac > 0.6
    ):
        return True
    return False


def run_capture(
    ppk: sb.PpkSession,
    post_ms: int,
    flash: bool,
    cold_wait_s: float,
) -> dict:
    kill_udp_9000()
    udp = UdpCollector()
    notes: list[str] = []
    status = "OK"
    leftover = False
    try:
        if flash:
            print(f"=== FLASH post_ms={post_ms} ===", flush=True)
            port = sb.try_flash_with_retries(ppk, BUILD, cold_wait_s)
            time.sleep(1.0)
            port2 = sb.wait_espressif_com(timeout_s=20.0) or port
            try:
                sb.hard_reset(port2)
            except Exception as e:
                notes.append(f"reset_warn={e}")
        else:
            print("=== POWER CYCLE (same firmware) ===", flush=True)
            ppk.power_cycle()
            port2 = sb.wait_espressif_com(timeout_s=max(35.0, cold_wait_s))
            if port2:
                try:
                    sb.hard_reset(port2)
                except Exception as e:
                    notes.append(f"reset_warn={e}")

        print("Waiting for warmup UDP idx=0 ...", flush=True)
        t_wait = time.time()
        got_warmup = False
        meas: StreamPpk | None = None
        t_idx10: float | None = None
        leftover_checked = False
        while time.time() - t_wait < 90.0:
            for idx in udp.poll():
                if idx == 0:
                    got_warmup = True
            if got_warmup:
                break
            time.sleep(0.05)
        if not got_warmup:
            return {
                "status": "NO_WARMUP",
                "notes": "no UDP idx=0 within 90s",
                "post_ms": post_ms,
                "udp": udp.summary(),
            }

        # Arm PPK during remaining warmup sleep so measured #1 wake is captured.
        time.sleep(2.0)
        meas = StreamPpk(ppk)
        meas.start()
        print("PPK measure armed (exclude warmup send)", flush=True)

        t_meas = time.time()
        deadline = t_meas + 12 * 60.0
        while time.time() < deadline:
            meas.drain()
            for idx in udp.poll():
                if idx == MEASURED:
                    t_idx10 = time.time()
            us = udp.summary()
            if (
                not leftover_checked
                and us["rx_unique"] >= 1
                and time.time() - t_meas > 40.0
            ):
                leftover_checked = True
                snap = meas.snapshot()
                print(
                    f"sleep_check sleep_avg_uA={snap.get('sleep_avg_uA')} "
                    f"frac={snap.get('sleep_frac')}",
                    flush=True,
                )
                if leftover_power(snap):
                    leftover = True
                    status = "LEFTOVER_POWER"
                    notes.append(
                        "sleep after Wi-Fi still elevated; stopping UDP campaign"
                    )
                    break
            if us["rx_unique"] >= MEASURED and t_idx10 is not None:
                if time.time() >= t_idx10 + PERIOD_S:
                    break
            time.sleep(0.001)
        meas.stop()
        # Drain UDP a little longer for stragglers.
        t_end = time.time() + 1.0
        while time.time() < t_end:
            udp.poll()
            time.sleep(0.05)

        us = udp.summary()
        snap = meas.snapshot() if meas else {}
        attempts, rx, loss = analyze_delivery(us)
        batt = battery_from_cycle(
            float(snap.get("charge_C") or 0.0),
            float(snap.get("sleep_avg_uA") or 0.0),
            VOLTAGE_MV / 1000.0,
        )
        if leftover:
            status = "LEFTOVER_POWER"
        elif rx < MEASURED:
            status = "LOSS"
        notes.append(
            f"rx={rx}/{attempts} sleep_avg_uA={snap.get('sleep_avg_uA')} "
            f"sleep_frac={snap.get('sleep_frac')}"
        )
        return {
            "status": status,
            "notes": "; ".join(notes),
            "post_ms": post_ms,
            "pre_ms": PRE_SETTLE_MS,
            "udp": us,
            "ppk": snap,
            "battery": batt,
            "attempts": attempts,
            "rx": rx,
            "loss_pct": loss,
            "utc": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        udp.close()


def write_outputs(data: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data["updated_utc"] = datetime.now(timezone.utc).isoformat()
    JSON_OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    lines = [
        "# Thermometer 2 UDP low-power 1-minute",
        "",
        f"- Repo: `{data.get('repo')}`",
        f"- Starting branch: `{data.get('starting_branch')}`",
        f"- Starting SHA: `{data.get('starting_sha')}`",
        f"- Branch: `{data.get('branch')}`",
        f"- HEAD: `{data.get('sha')}`",
        f"- CURRENT_FLASH_EXPECTED (before reflash): `{data.get('current_flash_expected')}`",
        f"- Voltage: {data.get('voltage_mv')} mV",
        f"- UDP: `{UDP_HOST}:{UDP_PORT}` LE uint32 index",
        f"- Started: {data.get('started_utc')}",
        f"- Updated: {data.get('updated_utc')}",
        "",
        "## Verdict",
        "",
        f"**{data.get('verdict', '')}**",
        "",
        data.get("verdict_detail", ""),
        "",
        "## Results",
        "",
        "| run | attempts | RX | loss | sleep µA | sleep med µA | total C | mC/cycle | total J | mJ/cycle | post_ms | status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    def fmt(x, n=2):
        return f"{x:.{n}f}" if isinstance(x, (int, float)) else ""

    for r in data.get("runs", []):
        p = r.get("ppk") or {}
        b = r.get("battery") or {}
        lines.append(
            f"| {r.get('name')} | {r.get('attempts')} | {r.get('rx')} | "
            f"{fmt(r.get('loss_pct'))}% | {fmt(p.get('sleep_avg_uA'))} | "
            f"{fmt(p.get('sleep_median_uA'))} | {fmt(p.get('charge_C'), 6)} | "
            f"{fmt(b.get('charge_per_cycle_mC'))} | {fmt(p.get('energy_J'), 6)} | "
            f"{fmt(b.get('energy_per_cycle_mJ'))} | {r.get('post_ms')} | "
            f"{r.get('status')} |"
        )
    energy = data.get("energy_from") or {}
    p = energy.get("ppk") or {}
    b = energy.get("battery") or {}
    lines.extend(
        [
            "",
            "## CACHE",
            "- channel=yes",
            "- BSSID=yes",
            "- static_ip=yes",
            "- peer_arp=yes",
            "- gateway_arp=yes",
            "",
            "## HOLDS",
            f"- pre={data.get('pre_ms', PRE_SETTLE_MS)} ms",
            f"- post={data.get('post_ms', '')} ms",
            "",
            "## POWERDOWN",
            "- GPIO2 PWR_ON = LOW+hold",
            "- GPIO17 LED_ON = LOW+hold",
            "- GPIO18 LED data = high-Z+hold",
            "- GPIO6 SDA = high-Z+hold",
            "- GPIO7 SCL = high-Z+hold",
            "- ULP/LP = stopped (BoardPowerDownForDeepSleep)",
            "- RTC_FAST/SLOW mem = ON (RTC_DATA_ATTR cache)",
            "",
            "## Energy (clean run)",
            f"- SLEEP_AVG_UA={fmt(p.get('sleep_avg_uA'))}",
            f"- SLEEP_MEDIAN_UA={fmt(p.get('sleep_median_uA'))}",
            f"- TOTAL_10_CHARGE_C={fmt(p.get('charge_C'), 6)}",
            f"- TOTAL_10_ENERGY_J={fmt(p.get('energy_J'), 6)}",
            f"- AVG_CHARGE_PER_INTERVAL_mC={fmt(b.get('charge_per_cycle_mC'))}",
            f"- AVG_ENERGY_PER_INTERVAL_mJ={fmt(b.get('energy_per_cycle_mJ'))}",
            f"- CR2 800 mAh life={fmt(b.get('life_days'))} d / {fmt(b.get('life_months'))} mo",
            f"- sleep-only mJ/cycle={fmt(b.get('sleep_energy_mJ'))}",
            f"- Wi-Fi/send mJ/cycle={fmt(b.get('wifi_energy_mJ'))}",
            "",
            "Deep-sleep current is from `|I|<200 µA` samples between bursts, "
            "not from the 10-minute overall average (which includes Wi-Fi).",
            "",
            "## Method",
            "1. `AETHER_DIAG_UDP_LOW_POWER_1MIN_10` silent build",
            "2. PPK OFF→ON 3.0 V, Espressif VID 0x303A, esptool flash",
            "3. Warmup #0 (not measured) then 10×60 s HOT UDP",
            "4. After send #10 include the last 60 s sleep; then DONE 1 h sleep",
            "",
        ]
    )
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voltage-mv", type=int, default=VOLTAGE_MV)
    ap.add_argument("--cold-wait-s", type=float, default=40.0)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--post-ms", type=int, default=POST_HOLDS_MS[0])
    args = ap.parse_args()

    sb.ensure_ppk_path()
    branch, sha = git_meta()
    data = {
        "repo": "temperature-sensor-prepared (origin aethernetio/temperature-sensor)",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "starting_branch": "diag/thermometer2-sleep-power-bisect",
        "starting_sha": "a2aa419c51ada9f092662a0be39207a0551826e1",
        "current_flash_expected": "B1_LED_ON_LOW",
        "branch": branch,
        "sha": sha,
        "voltage_mv": args.voltage_mv,
        "udp_host": UDP_HOST,
        "udp_port": UDP_PORT,
        "pre_ms": PRE_SETTLE_MS,
        "runs": [],
        "verdict": "IN_PROGRESS",
    }
    write_outputs(data)

    post_ms = args.post_ms
    ppk = sb.PpkSession(args.voltage_mv)
    ppk.open()
    try:
        consecutive = 0
        energy_run = None
        leftover_stop = False
        hold_idx = list(POST_HOLDS_MS).index(post_ms) if post_ms in POST_HOLDS_MS else 0
        while hold_idx < len(POST_HOLDS_MS):
            post_ms = POST_HOLDS_MS[hold_idx]
            data["post_ms"] = post_ms
            if not args.skip_build or not (BUILD / "flash_args").exists():
                print(f"=== BUILD post_ms={post_ms} ===", flush=True)
                build_firmware(post_ms)
            args.skip_build = False

            # Two consecutive 10/10 at this hold, flashing the first.
            run_results = []
            for i in range(1, 3):
                name = f"HOLD{post_ms}_RUN{i}"
                print(f"=== CAPTURE {name} ===", flush=True)
                result = run_capture(
                    ppk,
                    post_ms,
                    flash=(i == 1),
                    cold_wait_s=args.cold_wait_s,
                )
                result["name"] = name
                data["runs"] = [r for r in data["runs"] if r.get("name") != name]
                data["runs"].append(result)
                write_outputs(data)
                print(
                    f"RESULT {name} status={result.get('status')} "
                    f"rx={result.get('rx')}/{result.get('attempts')} "
                    f"sleep_avg_uA={(result.get('ppk') or {}).get('sleep_avg_uA')}",
                    flush=True,
                )
                run_results.append(result)
                if result.get("status") == "LEFTOVER_POWER":
                    leftover_stop = True
                    break
                rx_ok = result.get("status") == "OK" and result.get("rx") == MEASURED
                sleep_ua = (result.get("ppk") or {}).get("sleep_avg_uA")
                sleep_ok = isinstance(sleep_ua, (int, float)) and sleep_ua <= 25.0
                if not (rx_ok and sleep_ok):
                    break
            if leftover_stop:
                data["verdict"] = "FAIL_LEFTOVER_POWER"
                data["verdict_detail"] = (
                    "After Wi-Fi teardown, sleep current is still milliamp-class. "
                    "Stopped UDP campaign; GPIO OFF states were not changed."
                )
                break
            if (
                len(run_results) == 2
                and all(r.get("rx") == MEASURED and r.get("status") == "OK" for r in run_results)
            ):
                energy_run = run_results[1]
                data["verdict"] = "PASS"
                sleep_ua = (energy_run.get("ppk") or {}).get("sleep_avg_uA")
                target = (
                    "TARGET sleep ≤16 µA"
                    if isinstance(sleep_ua, (int, float)) and sleep_ua <= 16
                    else "GOOD sleep ≤25 µA"
                )
                data["verdict_detail"] = (
                    f"Two consecutive 10/10 runs at post_hold={post_ms} ms. {target}."
                )
                data["energy_from"] = energy_run
                data["post_ms"] = post_ms
                break
            # Loss: next post-send hold only.
            hold_idx += 1
            if hold_idx >= len(POST_HOLDS_MS):
                data["verdict"] = "FAIL"
                data["verdict_detail"] = (
                    "post-send hold 1000 ms still not 10/10 twice; stopped search."
                )
                if run_results:
                    data["energy_from"] = run_results[-1]
                    data["post_ms"] = post_ms
        if energy_run is None and data.get("runs"):
            data.setdefault("energy_from", data["runs"][-1])
        branch, sha = git_meta()
        data["branch"] = branch
        data["sha"] = sha
        write_outputs(data)
    finally:
        try:
            ppk.set_on()
        except Exception:
            pass
        ppk.close()

    write_outputs(data)
    print(f"VERDICT {data.get('verdict')}", flush=True)
    print(f"JSON {JSON_OUT}", flush=True)
    print(f"MD {MD_OUT}", flush=True)
    return 0 if data.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
