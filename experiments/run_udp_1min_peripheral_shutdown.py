#!/usr/bin/env python3
"""UDP 1-min + verified peripheral shutdown: build/flash/PPK measure.

Measures raw contiguous sleep (no ua<200 filter, no despike as result).
"""
from __future__ import annotations

import json
import os
import statistics
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402

BUILD = ROOT / "build-udp-1min-peripheral-shutdown"
RESULTS = HERE / "power_factor_results"
RAW_DIR = HERE / "power_modes_raw"
JSON_OUT = RESULTS / "udp_1min_peripheral_shutdown.json"
MD_OUT = HERE / "UDP_1MIN_PERIPHERAL_SHUTDOWN.md"
RAW_CSV = RAW_DIR / "udp_1min_peripheral_shutdown_raw.csv"

UDP_HOST = "192.168.68.84"
UDP_PORT = 9000
PRE_SETTLE_MS = 50
POST_HOLD_MS = 200
MEASURED = 10
PERIOD_S = 60.0
VOLTAGE_MV = 3000
CR2_MAH = 800.0
FS_HZ = 100_000.0
DT_S = 1.0 / FS_HZ

# Wake/sleep edge detection (not used for sleep averaging filter).
WAKE_ENTER_UA = 3000.0
WAKE_ENTER_N = 500  # 5 ms
SLEEP_ENTER_UA = 2000.0
SLEEP_ENTER_N = 20000  # 200 ms
GUARD_S = 0.5
SLEEP_VALIDATE_MAX_UA = 1000.0  # abort final if sleep mean still >1 mA


def wifi_creds() -> tuple[str, str]:
    # Same AP as prior Thermometer 2 UDP campaigns.
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
            if len(data) >= 4:
                (idx,) = struct.unpack_from("<I", data, 0)
            else:
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
        dups = {str(i): n for i, n in self.by_idx.items() if n > 1}
        return {
            "attempts": MEASURED,
            "rx_unique": len(unique),
            "rx_total": len(measured),
            "missing": missing,
            "duplicates": dups,
            "all_idx": [e["idx"] for e in self.events],
            "t_idx": {str(e["idx"]): e["t"] for e in self.events},
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


class RawCapture:
    """Stream PPK; online charge; decimated CSV (every 100th sample)."""

    def __init__(self, ppk: sb.PpkSession, csv_path: Path) -> None:
        self.ppk = ppk
        self.csv_path = csv_path
        self.n = 0
        self.sum_uA = 0.0
        self.charge_uC = 0.0
        self.wake_edges: list[float] = []
        self.sleep_edges: list[float] = []
        self.in_wake = False
        self.wake_run = 0
        self.sleep_run = 0
        self._fh = None
        self.t0_wall = 0.0
        self._decim = 0
        # Online segments for analysis without full RAM CSV
        self.seg_charge_uC = 0.0
        self.seg_sum_uA = 0.0
        self.seg_n = 0
        self.seg_t0 = 0.0
        self.active_segs: list[dict] = []
        self.sleep_segs: list[dict] = []

    def _close_seg(self, kind: str, t_end: float) -> None:
        if self.seg_n <= 0:
            return
        dur = max(DT_S, t_end - self.seg_t0)
        mean = self.seg_sum_uA / self.seg_n
        charge_C = self.seg_charge_uC / 1e6
        rec = {
            "ok": True,
            "t0": self.seg_t0,
            "t1": t_end,
            "duration_s": dur,
            "mean_uA": mean,
            "median_uA": mean,  # online path has no median
            "charge_C": charge_C,
            "charge_mC": charge_C * 1000.0,
            "avg_from_charge_uA": (charge_C / dur) * 1e6 if dur > 0 else mean,
            "samples": self.seg_n,
        }
        if kind == "active":
            self.active_segs.append(rec)
        else:
            self.sleep_segs.append(rec)
        self.seg_charge_uC = 0.0
        self.seg_sum_uA = 0.0
        self.seg_n = 0

    def start(self) -> None:
        assert self.ppk.ppk is not None
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.csv_path.open("w", encoding="utf-8", newline="\n")
        self._fh.write("t_s,uA\n")
        self.ppk.set_on()
        self.ppk.ppk.start_measuring()
        self.t0_wall = time.time()
        self.seg_t0 = 0.0
        self.in_wake = False

    def stop(self) -> None:
        assert self.ppk.ppk is not None
        t = self.n * DT_S
        if self.in_wake:
            self._close_seg("active", t)
        else:
            self._close_seg("sleep", t)
        try:
            self.ppk.ppk.stop_measuring()
        except Exception as e:
            print(f"ppk_stop_warn={e}", flush=True)
        if self._fh:
            self._fh.close()
            self._fh = None

    def drain(self) -> None:
        assert self.ppk.ppk is not None and self._fh is not None
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
        lines: list[str] = []
        for uA in chunk:
            u = float(uA)
            t = self.n * DT_S
            self.n += 1
            self.sum_uA += u
            self.charge_uC += u * DT_S
            self.seg_sum_uA += u
            self.seg_charge_uC += u * DT_S
            self.seg_n += 1
            self._decim += 1
            if self._decim >= 100:
                self._decim = 0
                lines.append(f"{t:.6f},{u:.4f}\n")
            au = abs(u)
            if not self.in_wake:
                if u >= WAKE_ENTER_UA:
                    self.wake_run += 1
                else:
                    self.wake_run = 0
                if self.wake_run >= WAKE_ENTER_N:
                    # end sleep seg, start active
                    t_edge = t
                    self._close_seg("sleep", t_edge)
                    self.in_wake = True
                    self.wake_run = 0
                    self.sleep_run = 0
                    self.wake_edges.append(t_edge)
                    self.seg_t0 = t_edge
            else:
                if au <= SLEEP_ENTER_UA:
                    self.sleep_run += 1
                else:
                    self.sleep_run = 0
                if self.sleep_run >= SLEEP_ENTER_N:
                    t_edge = t - SLEEP_ENTER_N * DT_S
                    self._close_seg("active", t_edge)
                    self.in_wake = False
                    self.sleep_run = 0
                    self.sleep_edges.append(t_edge)
                    self.seg_t0 = t_edge
        if lines:
            self._fh.writelines(lines)


def load_csv(path: Path) -> tuple[np.ndarray, np.ndarray]:
    # May be large; load as float32.
    data = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.float64)
    if data.ndim == 1:
        return data[0:1], data[1:2]
    return data[:, 0], data[:, 1]


def window_stats(ts: np.ndarray, us: np.ndarray, t0: float, t1: float) -> dict:
    m = (ts >= t0) & (ts < t1)
    if not np.any(m):
        return {"ok": False}
    w_t = ts[m]
    w_u = us[m]
    dur = float(w_t[-1] - w_t[0]) if len(w_t) > 1 else 0.0
    if dur <= 0:
        return {"ok": False}
    mean = float(np.mean(w_u))
    charge_C = float(np.trapz(w_u, w_t) * 1e-6)
    from_q = (charge_C / dur) * 1e6
    return {
        "ok": True,
        "t0": t0,
        "t1": t1,
        "duration_s": dur,
        "mean_uA": mean,
        "median_uA": float(np.median(w_u)),
        "charge_C": charge_C,
        "charge_mC": charge_C * 1000.0,
        "avg_from_charge_uA": from_q,
        "rel_err": abs(mean - from_q) / max(abs(mean), 1.0),
        "samples": int(len(w_u)),
    }


def analyze_capture(
    cap: RawCapture,
    udp_summary: dict,
) -> dict:
    total_dur = cap.n * DT_S
    total_charge_C = cap.charge_uC / 1e6

    # Skip leading partial sleep before first wake; use measured cycles.
    actives = list(cap.active_segs)
    sleeps = list(cap.sleep_segs)
    # If first active is warmup, drop it for ACTIVE stats (keep sleeps after measured).
    if len(actives) > MEASURED:
        actives = actives[-MEASURED:]
    if len(sleeps) > MEASURED:
        sleeps = sleeps[-MEASURED:]
    # Prefer pairing: after dropping warmup active, take next MEASURED sleeps
    if len(cap.active_segs) > MEASURED and len(cap.sleep_segs) >= MEASURED:
        # sleeps that follow measured actives
        sleeps = cap.sleep_segs[-MEASURED:]
        actives = cap.active_segs[-MEASURED:]

    def agg_mc(items: list[dict]) -> dict:
        vals = [x["charge_mC"] for x in items if x.get("ok")]
        if not vals:
            return {"mean": None, "median": None, "min": None, "max": None, "n": 0}
        return {
            "mean": float(statistics.mean(vals)),
            "median": float(statistics.median(vals)),
            "min": float(min(vals)),
            "max": float(max(vals)),
            "n": len(vals),
        }

    def agg_ua(items: list[dict]) -> dict:
        vals = [x["mean_uA"] for x in items if x.get("ok")]
        if not vals:
            return {"mean": None, "median": None, "worst": None, "n": 0}
        return {
            "mean": float(statistics.mean(vals)),
            "median": float(statistics.median(vals)),
            "worst": float(max(vals)),
            "n": len(vals),
        }

    n_cyc = min(len(actives), len(sleeps), MEASURED)
    life: dict = {}
    if n_cyc >= 1:
        q_sum = sum(a["charge_C"] for a in actives[:n_cyc]) + sum(
            s["charge_C"] for s in sleeps[:n_cyc]
        )
        q_per = q_sum / n_cyc
        i_avg_uA = (q_per / PERIOD_S) * 1e6
        i_avg_mA = i_avg_uA / 1000.0
        life_h = (CR2_MAH / i_avg_mA) if i_avg_mA > 0 else float("inf")
        life = {
            "charge_per_1min_cycle_mC": q_per * 1000.0,
            "average_current_uA": i_avg_uA,
            "life_hours": life_h,
            "life_days": life_h / 24.0,
            "life_months": life_h / 24.0 / 30.44,
            "n_cycles": n_cyc,
            "total_cycles_charge_C": q_sum,
            "duration_s": sum(a["duration_s"] for a in actives[:n_cyc])
            + sum(s["duration_s"] for s in sleeps[:n_cyc]),
        }

    return {
        "udp": udp_summary,
        "capture": {
            "samples": cap.n,
            "duration_s": total_dur,
            "total_charge_C": total_charge_C,
            "avg_uA": (cap.sum_uA / cap.n) if cap.n else None,
            "wake_edges": cap.wake_edges,
            "sleep_edges": cap.sleep_edges,
            "raw_csv_decimated": str(cap.csv_path),
        },
        "active": {
            "mean_mC": agg_mc(actives)["mean"],
            "median_mC": agg_mc(actives)["median"],
            "min_mC": agg_mc(actives)["min"],
            "max_mC": agg_mc(actives)["max"],
            "n": agg_mc(actives)["n"],
            "windows_n": len(actives),
        },
        "sleep_raw": {
            "mean_uA": agg_ua(sleeps)["mean"],
            "median_uA": agg_ua(sleeps)["median"],
            "worst_uA": agg_ua(sleeps)["worst"],
            "n": agg_ua(sleeps)["n"],
            "windows_n": len(sleeps),
            "filtering_used": False,
            "despike_used_for_result": False,
            "note": "contiguous online integrate; spikes included in mean",
        },
        "full_cycle": life,
        "csv": str(cap.csv_path),
    }


def validate_sleep(ppk: sb.PpkSession, udp: UdpCollector) -> dict:
    """After flash: wait measured UDP #1, then sample contiguous sleep (~15s)."""
    print("=== VALIDATION: wait measured UDP idx=1 then sleep ===", flush=True)
    t0 = time.time()
    saw1 = False
    while time.time() - t0 < 180.0:
        for idx in udp.poll():
            if idx >= 1:
                saw1 = True
                break
        if saw1:
            break
        time.sleep(0.05)
    if not saw1:
        return {"ok": False, "reason": "no_udp_idx1"}
    # Teardown + enter sleep (~1–2 s), then sample well before next 60 s wake.
    time.sleep(3.0)
    csv = RESULTS / "udp_shutdown_validate_sleep.csv"
    assert ppk.ppk is not None
    ppk.ppk.start_measuring()
    samples: list[float] = []
    t_end = time.time() + 15.0
    while time.time() < t_end:
        try:
            raw = ppk.ppk.get_data()
            if raw:
                chunk, _ = ppk.ppk.get_samples(raw)
                samples.extend(float(x) for x in chunk)
        except Exception as e:
            print(f"val_drain_warn={e}", flush=True)
        time.sleep(0.01)
        udp.poll()
    try:
        ppk.ppk.stop_measuring()
    except Exception:
        pass
    if len(samples) < 1000:
        return {"ok": False, "reason": "few_samples", "n": len(samples)}
    arr = np.asarray(samples, dtype=float)
    skip = int(FS_HZ * 1.0)
    win = arr[skip:] if len(arr) > skip else arr
    mean = float(np.mean(win))
    med = float(np.median(win))
    csv.parent.mkdir(parents=True, exist_ok=True)
    with csv.open("w", encoding="utf-8") as f:
        f.write("uA\n")
        for u in win[::100]:
            f.write(f"{u:.4f}\n")
    ok = med < SLEEP_VALIDATE_MAX_UA
    frac_low = float(np.mean(np.abs(win) < SLEEP_VALIDATE_MAX_UA))
    print(
        f"VALIDATE sleep_mean_uA={mean:.1f} median={med:.1f} "
        f"frac_lt_1mA={frac_low:.3f} ok={ok}",
        flush=True,
    )
    return {
        "ok": ok,
        "mean_uA": mean,
        "median_uA": med,
        "frac_lt_1mA": frac_low,
        "samples": len(win),
        "csv": str(csv),
        "note": "gate uses median; mean may include PPK autorange spikes",
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
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
        port = sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=30.0)
        time.sleep(1.0)
        port2 = sb.wait_espressif_com(timeout_s=20.0) or port
        try:
            sb.hard_reset(port2)
        except Exception as e:
            print(f"reset_warn={e}", flush=True)

        val = validate_sleep(ppk, udp)
        if not val.get("ok"):
            out = {
                "status": "VALIDATION_FAIL",
                "validation": val,
                "branch": branch,
                "sha": sha,
            }
            JSON_OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
            print("VALIDATION FAIL — abort final 10x60", flush=True)
            return 2

        # Power-cycle for clean final capture (RTC clear optional — keep caches)
        print("=== FINAL 10x60 CAPTURE ===", flush=True)
        kill_udp_9000()
        udp.close()
        udp = UdpCollector()
        ppk.power_cycle()
        port3 = sb.wait_espressif_com(timeout_s=40.0)
        if port3:
            try:
                sb.hard_reset(port3)
            except Exception as e:
                print(f"reset_warn={e}", flush=True)

        cap = RawCapture(ppk, RAW_CSV)
        cap.start()
        t_start = time.time()
        # Warmup + 10 measured + 10th sleep (~8 + 10*60 + 55)
        deadline = t_start + 8 + MEASURED * PERIOD_S + 70
        last_idx = -1
        got_10 = False
        t_idx10 = None
        while time.time() < deadline:
            for idx in udp.poll():
                last_idx = idx
                if idx == MEASURED:
                    got_10 = True
                    t_idx10 = time.time()
            cap.drain()
            if got_10 and t_idx10 and (time.time() - t_idx10) > 55.0:
                # complete ~remaining sleep of cycle 10
                break
            time.sleep(0.01)
        # drain a bit more
        t_extra = time.time() + 2.0
        while time.time() < t_extra:
            cap.drain()
            udp.poll()
            time.sleep(0.01)
        cap.stop()
        udp_sum = udp.summary()
        print(f"UDP summary={udp_sum}", flush=True)

        analysis = analyze_capture(cap, udp_sum)
        analysis["validation"] = val
        analysis["branch"] = branch
        analysis["sha"] = sha
        analysis["gpio"] = {
            "GPIO17": "LOW+hold",
            "GPIO2": "HIGH+hold",
            "GPIO18": "DISABLED",
            "GPIO6": "DISABLED",
            "GPIO7": "DISABLED",
        }
        analysis["wifi"] = {
            "PRE_ms": PRE_SETTLE_MS,
            "POST_ms": POST_HOLD_MS,
            "target": f"{UDP_HOST}:{UDP_PORT}",
            "cache_changed": False,
        }
        analysis["status"] = "OK"
        analysis["utc"] = datetime.now(timezone.utc).isoformat()

        compact = analysis
        JSON_OUT.write_text(json.dumps(compact, indent=2), encoding="utf-8")

        fc = analysis.get("full_cycle") or {}
        sr = analysis.get("sleep_raw") or {}
        ac = analysis.get("active") or {}
        lines = [
            "# UDP 1-min + verified peripheral shutdown",
            "",
            f"- branch: `{branch}`",
            f"- sha: `{sha}`",
            f"- UDP: attempts={udp_sum['attempts']} RX={udp_sum['rx_unique']} "
            f"missing={udp_sum['missing']} dups={udp_sum['duplicates']}",
            f"- GPIO: 17=LOW 2=HIGH 18/6/7=DISABLED",
            f"- active mean_mC={ac.get('mean_mC')} median={ac.get('median_mC')}",
            f"- sleep raw mean_uA={sr.get('mean_uA')} median={sr.get('median_uA')} "
            f"worst={sr.get('worst_uA')} (no filter/despike)",
            f"- Q/cycle_mC={fc.get('charge_per_1min_cycle_mC')} "
            f"Iavg_uA={fc.get('average_current_uA')}",
            f"- CR2 life_days={fc.get('life_days')}",
            f"- raw CSV (decimated): `{RAW_CSV}` (not committed)",
            "",
        ]
        MD_OUT.write_text("\n".join(lines), encoding="utf-8")
        print(json.dumps(compact, indent=2), flush=True)
        return 0 if udp_sum["rx_unique"] >= 8 else 3
    finally:
        try:
            udp.close()
        except Exception:
            pass
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
