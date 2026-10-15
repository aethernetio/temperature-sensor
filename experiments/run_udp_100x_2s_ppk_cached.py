#!/usr/bin/env python3
"""UDP 100x / 2 s / PPK: cached low-power path (min energy among UDP diag builds).

Destination is this PC on the LAN (not 127.0.0.1 on the ESP). Wi-Fi cache ON.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_udp_100x_1min_ppk_baseline as base  # noqa: E402
import run_thermometer2_sleep_bisect as sb  # noqa: E402

PERIOD_S = 2.0
MEASURED = 100

BUILD = ROOT / "build-udp-100x-2s-ppk"
RESULTS = HERE / "power_factor_results"
ART_DIR = RESULTS / "udp_100x_2s_ppk_windows"
CSV_OUT = RESULTS / "udp_100x_2s_ppk_sends.csv"
JSON_OUT = RESULTS / "udp_100x_2s_ppk_baseline.json"
MD_OUT = HERE / "UDP_100X_2S_PPK_CACHED.md"


def detect_lan_host() -> str:
    """IPv4 of this machine reachable from the Wi-Fi LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.68.1", 1))
        return s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


class SendBurstStreamer2s(base.SendBurstStreamer):
    """PPK windows for short period: cap cycle end before the next UDP RX."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.udp_t: dict[int, float] = {}

    def note_udp(self, idx: int, wall_t: float | None = None) -> None:
        import time

        t_rel = (wall_t if wall_t is not None else time.time()) - self.t0_wall
        if 1 <= idx <= MEASURED:
            self.udp_t[idx] = t_rel
        super().note_udp(idx, wall_t)

    def _segment_one(self, idx: int, t_udp: float) -> dict | None:
        import numpy as np

        t_arr, u_arr = self._arrays()
        if len(t_arr) < 1000:
            return None
        t0 = self._find_wake_before(t_arr, u_arr, t_udp)
        t1 = self._find_sleep_after(t_arr, u_arr, t_udp)
        t_next = self.udp_t.get(idx + 1)
        if t_next is not None:
            cap = max(t_udp + 0.15, t_next - 0.2)
            if t1 is None or t1 > cap:
                t1 = cap
        if t0 is None or t1 is None or t1 <= t0:
            return None
        dur = t1 - t0
        m = (t_arr >= t0) & (t_arr <= t1)
        if not np.any(m):
            return None
        wt = t_arr[m]
        wu = u_arr[m]
        try:
            integ_uA_s = float(np.trapezoid(wu, wt))
        except AttributeError:
            integ_uA_s = float(np.trapz(wu, wt))
        charge_mC = abs(integ_uA_s) / 1000.0
        duration_ms = dur * 1000.0
        peak_mA = float(np.max(np.abs(wu))) / 1000.0
        mean_uA = float(np.mean(np.abs(wu)))
        avg_from_charge = (charge_mC * 1000.0) / dur if dur > 0 else 0.0
        rel_err = abs(mean_uA - avg_from_charge) / avg_from_charge if avg_from_charge else 0.0
        valid = (
            base.MIN_ACTIVE_S <= dur <= base.MAX_ACTIVE_S
            and charge_mC <= 600.0
            and rel_err <= base.CROSS_CHECK_REL_ERR
        )
        rec = {
            "valid": valid,
            "reason": "" if valid else "out_of_range",
            "sequence_detect": idx,
            "t0": t0,
            "t1": t1,
            "duration_ms": duration_ms,
            "charge_mC": charge_mC,
            "energy_mJ": charge_mC * base.VOLTAGE_V,
            "peak_current_mA": peak_mA,
            "mean_uA": mean_uA,
            "avg_from_charge_uA": avg_from_charge,
            "rel_err": rel_err,
            "samples": int(len(wt)),
            "wall_t0": self.t0_wall + t0,
            "wall_t1": self.t0_wall + t1,
            "raw_window": None,
        }
        if base.SAVE_WINDOW_RAW and valid:
            path = self.art_dir / f"send_{idx:03d}.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                import csv

                w = csv.writer(f)
                w.writerow(["t_s", "uA"])
                for t, u in zip(wt, wu):
                    w.writerow([t, u])
            rec["raw_window"] = str(path)
        print(
            f"SEND_WIN seq={idx} valid={valid} dur_ms={duration_ms:.0f} "
            f"charge_mC={charge_mC:.1f}",
            flush=True,
        )
        return rec


def patch_base(udp_host: str) -> None:
    base.BUILD = BUILD
    base.RESULTS = RESULTS
    base.ART_DIR = ART_DIR
    base.CSV_OUT = CSV_OUT
    base.JSON_OUT = JSON_OUT
    base.MD_OUT = MD_OUT
    base.UDP_HOST = udp_host
    base.PERIOD_S = PERIOD_S
    base.MEASURED = MEASURED
    base.UDP_SEGMENT_DELAY_S = 0.35
    base.MAX_ACTIVE_S = 8.0
    base.SendBurstStreamer = SendBurstStreamer2s
    # Shorter post-RX wait before next wake at 2 s cadence.
    orig_build = base.build_firmware

    def build_firmware() -> None:
        BUILD.mkdir(parents=True, exist_ok=True)
        ssid, password = base.wifi_creds()
        b = BUILD.as_posix()
        defs = (
            f"-D AETHER_DIAG_UDP_LOW_POWER_1MIN_10=1 "
            f"-D BOARD=0 "
            f"-D WIFI_SSID={ssid} "
            f"-D WIFI_PASSWORD={password} "
            f"-D AE_UDP_SERVER_HOST={udp_host} "
            f"-D AE_UDP_SERVER_PORT={base.UDP_PORT} "
            f"-D AE_UDP_PRE_SETTLE_MS={base.PRE_SETTLE_MS} "
            f"-D AE_UDP_POST_SEND_HOLD_MS={base.POST_HOLD_MS} "
            f"-D AE_UDP_MEASURED_SENDS={MEASURED} "
            f"-D AE_UDP_WIFI_CACHE=1 "
            f"-D AE_UDP_PERIOD_S={int(PERIOD_S)} "
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

    base.build_firmware = build_firmware

    orig_run_final = base.run_final_100

    def run_final_100(ppk, udp):
        print("=== FINAL 100x2s CAPTURE ===", flush=True)
        if not base.wait_udp_idx(udp, 0, 150.0, "final warmup"):
            raise RuntimeError("final: no UDP warmup idx=0 after cold boot")
        import time

        time.sleep(4.0)
        stream = base.SendBurstStreamer(ppk, ART_DIR / "final")
        stream.start()
        t_start = time.time()
        deadline = t_start + MEASURED * PERIOD_S + 900.0
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
            if got_last and t_last and (time.time() - t_last) > 6.0:
                if len(stream.done_idx) >= MEASURED or (time.time() - t_last) > 25.0:
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

    base.run_final_100 = run_final_100

    orig_write_md = base.write_md

    def write_md(rep: dict) -> None:
        orig_write_md(rep)
        text = MD_OUT.read_text(encoding="utf-8")
        text = text.replace("1 MIN", "2 S").replace("1min", "2s")
        text = (
            text.replace(
                "# UDP 100x / 1 MIN / PPK BASELINE",
                "# UDP 100x / 2 S / PPK CACHED LOW-POWER",
            )
            + f"\n- wifi_cache: 1\n- period_s: {PERIOD_S}\n- udp_host: {udp_host}\n"
        )
        MD_OUT.write_text(text, encoding="utf-8")

    base.write_md = write_md


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--udp-host", default="", help="LAN IP of this PC (auto if empty)")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--final-only", action="store_true")
    args, rest = ap.parse_known_args()
    if rest:
        print(f"ignored extra args: {rest}", flush=True)

    host = args.udp_host.strip() or detect_lan_host()
    print(f"udp_destination={host}:{base.UDP_PORT} period_s={PERIOD_S}", flush=True)
    patch_base(host)
    sys.argv = [sys.argv[0]]
    if args.skip_build:
        sys.argv.append("--skip-build")
    if args.final_only:
        sys.argv.append("--final-only")
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
