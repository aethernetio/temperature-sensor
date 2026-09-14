#!/usr/bin/env python3
"""Thermometer 2 deep-sleep current bisect: build/flash/reset/measure via PPK2.

Owns the PPK2 source-meter session for each variant so DUT power never drops
between flash and sleep measurement. Flashes only via Espressif VID 0x303A.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = HERE / "power_factor_results"
JSON_OUT = RESULTS / "thermometer2_sleep_bisect.json"
MD_OUT = HERE / "THERMOMETER2_DEEP_SLEEP_POWER_BISECT.md"
PPK_SITE = HERE / "ppk2-venv" / "Lib" / "site-packages"
IDF_PATH = Path(r"C:\Espressif\frameworks\esp-idf-v6.0.2")
IDF_PYTHON = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
EXPORT_PS1 = IDF_PATH / "export.ps1"

ESPRESSIF_VID = 0x303A

VARIANTS_DEFAULT = [
    "M0_MINIMAL",
    "H1_PWR_HIGH",
    "A0_BASELINE_NOPIN",
    "A1_PWR_LOW",
    "A2_PWR_HOLD",
    "A3_PWR_ISOLATE",
    "B1_LED_ON_LOW",
    "B2_LED_ON_HIGH",
    "B3_LED_DATA_HZ",
    "C1_I2C_HZ",
    "D1_ULP_STOP",
    "E1_PD_MIN",
    "E2_RTC_MEM_ON",
    "E3_RTC_MEM_AUTO",
    "Z_FULL_QUIET",
]


def ensure_ppk_path() -> None:
    sp = str(PPK_SITE)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, **kw)


def stop_external_ppk_holders() -> None:
    """Kill detached hold/log processes so this script can own the PPK2."""
    pid_file = HERE / "ppk2_hold.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="ascii").strip())
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False
            )
        except Exception:
            pass
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'ppk2_(hold_power|log_power|integrate_run|power\\.py)' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            capture_output=True,
            check=False,
        )
    time.sleep(1.0)


class PpkSession:
    def __init__(self, voltage_mv: int) -> None:
        ensure_ppk_path()
        from ppk2_api.ppk2_api import PPK2_API

        self.PPK2_API = PPK2_API
        self.voltage_mv = voltage_mv
        self.ppk = None

    def open(self) -> None:
        stop_external_ppk_holders()
        devices = self.PPK2_API.list_devices()
        if not devices:
            raise RuntimeError("No PPK2 found")
        port = devices[0][0]
        print(f"PPK2_PORT={port}", flush=True)
        self.ppk = self.PPK2_API(port)
        self.ppk.__del__ = lambda *a, **k: None  # type: ignore[method-assign]
        self.ppk.get_modifiers()
        # Nordic PPK2 range-switch "phantom spikes" dominate raw mean on µA floors
        # unless spike smoothing is stronger than the library defaults (3 / 0.18).
        # Match a heavier GUI Advanced spike-filter so contiguous Q/T reflects DUT.
        try:
            self.ppk.spike_filter_samples = 25
            self.ppk.spike_filter_alpha = 0.08
            self.ppk.spike_filter_alpha5 = 0.03
            print(
                "PPK_SPIKE_FILTER samples=25 alpha=0.08 alpha5=0.03",
                flush=True,
            )
        except Exception as e:
            print(f"ppk_spike_filter_warn={e}", flush=True)
        self.set_off()
        time.sleep(1.5)
        self.set_on()

    def set_off(self) -> None:
        assert self.ppk is not None
        self.ppk.set_source_voltage(self.voltage_mv)
        self.ppk.use_source_meter()
        self.ppk.toggle_DUT_power("OFF")
        print("DUT_POWER=OFF", flush=True)

    def set_on(self) -> None:
        assert self.ppk is not None
        self.ppk.set_source_voltage(self.voltage_mv)
        self.ppk.use_source_meter()
        self.ppk.toggle_DUT_power("ON")
        print(f"DUT_POWER=ON mv={self.voltage_mv}", flush=True)

    def power_cycle(self) -> None:
        self.set_off()
        time.sleep(2.0)
        self.set_on()

    def measure(self, duration_s: float, out_csv: Path) -> dict:
        assert self.ppk is not None
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        # Keep asserting ON.
        self.set_on()
        self.ppk.start_measuring()
        samples: list[tuple[float, float]] = []
        t0 = time.time()
        last = t0
        try:
            while time.time() - t0 < duration_s:
                try:
                    raw = self.ppk.get_data()
                except OSError as e:
                    print(f"ppk_get_data_warn={e}", flush=True)
                    break
                now = time.time()
                if raw:
                    try:
                        chunk, _ = self.ppk.get_samples(raw)
                    except Exception as e:
                        print(f"ppk_get_samples_warn={e}", flush=True)
                        break
                    for uA in chunk:
                        samples.append((now - t0, float(uA)))
                if now - last >= 5.0:
                    if samples:
                        recent = [u for _, u in samples if _ >= max(0.0, now - t0 - 5)]
                        avg = statistics.fmean(recent) if recent else 0.0
                        print(
                            f"meas samples={len(samples)} recent_avg_uA={avg:.1f} "
                            f"elapsed={now - t0:.1f}s",
                            flush=True,
                        )
                    last = now
                else:
                    time.sleep(0.001)
        finally:
            try:
                self.ppk.stop_measuring()
            except Exception as e:
                print(f"ppk_stop_warn={e}", flush=True)

        try:
            step = max(1, len(samples) // 20000) if samples else 1
            with out_csv.open("w", encoding="utf-8", newline="") as f:
                f.write("t_s,uA\n")
                for i, (t, u) in enumerate(samples):
                    if i % step == 0:
                        f.write(f"{t:.6f},{u:.3f}\n")
        except OSError as e:
            print(f"csv_write_warn={e}", flush=True)

        # Contiguous window: drop only the first settle_s seconds (enter sleep),
        # then use EVERY sample — no |I|<200 filtering (that hid ~8 mA as ~8 µA).
        settle_s = 5.0
        window = [(t, u) for t, u in samples if t >= settle_s]
        if len(window) < 100:
            window = list(samples)
        if not window:
            raise RuntimeError("no current samples")
        ts = [t for t, _ in window]
        us = [u for _, u in window]
        duration_s = max(1e-9, ts[-1] - ts[0]) if len(ts) > 1 else float("nan")
        # Trapezoid charge (A·s). Uneven sample times → use recorded timestamps.
        charge_C = 0.0
        for i in range(1, len(ts)):
            dt = ts[i] - ts[i - 1]
            if dt > 0:
                charge_C += 0.5 * (us[i] + us[i - 1]) * 1e-6 * dt
        avg_uA = statistics.fmean(us)
        from_q_uA = (charge_C / duration_s) * 1e6 if duration_s > 0 else float("nan")
        rel = abs(avg_uA - from_q_uA) / max(abs(avg_uA), 1.0)
        q_ok = (rel < 0.05 or abs(avg_uA - from_q_uA) < 1.0) and not (
            duration_s >= 5.0 and charge_C >= 0.05 and avg_uA < 100.0
        )
        use_sorted = sorted(us)
        mid = len(use_sorted) // 2
        median = (
            use_sorted[mid]
            if len(use_sorted) % 2
            else 0.5 * (use_sorted[mid - 1] + use_sorted[mid])
        )
        # Diagnostic only — never used as pass/fail sleep current.
        filtered = [u for u in us if abs(u) < 200.0]
        filt_avg = statistics.fmean(filtered) if filtered else float("nan")
        return {
            "samples": len(us),
            "avg_uA": avg_uA,
            "median_uA": median,
            "min_uA": min(us),
            "max_uA": max(us),
            "stdev_uA": statistics.pstdev(us) if len(us) > 1 else 0.0,
            "sleep_avg_uA": avg_uA,  # RAW contiguous mean (pass/fail)
            "sleep_median_uA": median,
            "sleep_frac": 1.0,
            "raw_contiguous": {
                "duration_s": duration_s,
                "charge_C": charge_C,
                "charge_mC": charge_C * 1000.0,
                "avg_uA": avg_uA,
                "avg_from_charge_uA": from_q_uA,
                "q_over_t_rel_err": rel,
                "q_over_t_ok": q_ok,
                "sample_filter_used": False,
                "diag_filtered_lt200_avg_uA": filt_avg,
            },
            "csv": str(out_csv),
        }

    def close(self) -> None:
        if self.ppk is not None:
            try:
                if self.ppk.ser and self.ppk.ser.is_open:
                    self.ppk.ser.close()
            except Exception:
                pass
            self.ppk = None


def list_espressif_ports() -> list[str]:
    ensure_ppk_path()
    from serial.tools import list_ports

    ports: list[str] = []
    for p in list_ports.comports():
        if p.vid == ESPRESSIF_VID:
            ports.append(p.device)
            print(
                f"found ESPRESSIF {p.device} VID={p.vid:04X} PID={p.pid:04X} {p.description}",
                flush=True,
            )
        elif p.vid is not None:
            print(
                f"ignore {p.device} VID={p.vid:04X} PID={p.pid:04X} {p.description}",
                flush=True,
            )
    return ports


def wait_espressif_com(timeout_s: float = 45.0) -> str | None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        ports = list_espressif_ports()
        if ports:
            print(f"ESPRESSIF_COM={ports[0]}", flush=True)
            return ports[0]
        time.sleep(0.5)
    print("NO_ESPRESSIF_COM", flush=True)
    return None


def idf_cmd(extra: str) -> list[str]:
    # Use export.ps1 from IDF v6 tree. Clear PYTHONPATH so ppk2-venv site-packages
    # (e.g. pyparsing) cannot break IDF dependency checks.
    ps = f"""
$ErrorActionPreference = 'Stop'
$env:IDF_PATH = '{IDF_PATH.as_posix()}'
$env:PYTHONPATH = $null
$env:ESP_IDF_EXPORT_DEBUG = $null
. '{EXPORT_PS1.as_posix()}'
Set-Location '{ROOT.as_posix()}'
{extra}
exit $LASTEXITCODE
"""
    return [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps,
    ]


def build_variant(variant: str, build_dir: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    b = build_dir.as_posix()
    # Reuse one build tree; only variant define changes → fast incremental rebuild.
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py -B '{b}' -D AETHER_DIAG_SLEEP_POWER_BISECT=1 -D AE_SLEEP_BISECT_VARIANT={variant} -D BOARD=0 set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py -B '{b}' -D AETHER_DIAG_SLEEP_POWER_BISECT=1 -D AE_SLEEP_BISECT_VARIANT={variant} -D BOARD=0 build
"""
    r = run(idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"build failed variant={variant} code={r.returncode}")


def flash_port(port: str, build_dir: Path) -> None:
    """Flash via esptool directly (no export.ps1) while COM is still alive."""
    ensure_ppk_path()
    import serial

    # Hold chip in reset immediately so it cannot deep-sleep before flash.
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
        ser.setDTR(False)
        ser.setRTS(True)  # reset held
        time.sleep(0.05)
        ser.close()
    except Exception as e:
        print(f"reset_hold_warn={e}", flush=True)

    flash_args = build_dir / "flash_args"
    if not flash_args.exists():
        raise RuntimeError(f"missing flash_args in {build_dir}")

    cmd = [
        str(IDF_PYTHON),
        "-m",
        "esptool",
        "--chip",
        "esp32c6",
        "-p",
        port,
        "-b",
        "460800",
        "--before",
        "default-reset",
        "--after",
        "hard-reset",
        "write-flash",
        "@flash_args",
    ]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = run(cmd, cwd=str(build_dir), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"esptool flash failed port={port} code={r.returncode}")


def hard_reset(port: str) -> None:
    ensure_ppk_path()
    import serial

    with serial.Serial(port, 115200, timeout=0.2) as ser:
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.1)
        ser.setRTS(False)
        time.sleep(0.05)


def try_flash_with_retries(
    ppk: PpkSession, build_dir: Path, cold_wait_s: float, attempts: int = 4
) -> str:
    last_err = "no attempts"
    for i in range(attempts):
        print(f"=== FLASH attempt {i + 1}/{attempts} ===", flush=True)
        ppk.power_cycle()
        port = wait_espressif_com(timeout_s=max(35.0, cold_wait_s))
        if not port:
            last_err = "NO_COM"
            continue
        # Flash ASAP — do not run export.ps1.
        try:
            flash_port(port, build_dir)
            return port
        except Exception as e:
            last_err = str(e)
            print(f"flash_attempt_fail={e}", flush=True)
            time.sleep(1.0)
    raise RuntimeError(last_err)


def load_results() -> dict:
    if JSON_OUT.exists():
        return json.loads(JSON_OUT.read_text(encoding="utf-8"))
    return {
        "repo": "temperature-sensor-prepared (origin aethernetio/temperature-sensor)",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "voltage_mv": 3000,
        "variants": [],
    }


def save_results(data: dict, write_markdown: bool = True) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data["updated_utc"] = datetime.now(timezone.utc).isoformat()
    JSON_OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if write_markdown:
        write_md(data)


def write_md(data: dict) -> None:
    lines = [
        "# Thermometer 2 deep-sleep power bisect",
        "",
        f"- Repo used: `{data.get('repo')}`",
        f"- Branch: `{data.get('branch', '')}`",
        f"- HEAD: `{data.get('sha', '')}`",
        f"- Backup: `{data.get('original_backup', '')}`",
        f"- Voltage: {data.get('voltage_mv')} mV (PPK2 source meter)",
        f"- Started: {data.get('started_utc')}",
        f"- Updated: {data.get('updated_utc')}",
        "",
        "## Results",
        "",
        "| Variant | sleep_avg_uA | sleep_med_uA | median_uA | avg_uA | frac | status | notes |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for v in data.get("variants", []):
        m = v.get("measure") or {}

        def fmt(x):
            return f"{x:.2f}" if isinstance(x, (int, float)) else ""

        lines.append(
            f"| {v.get('name')} | {fmt(m.get('sleep_avg_uA'))} | "
            f"{fmt(m.get('sleep_median_uA'))} | {fmt(m.get('median_uA'))} | "
            f"{fmt(m.get('avg_uA'))} | {fmt(m.get('sleep_frac'))} | "
            f"{v.get('status', '')} | {v.get('notes', '')} |"
        )
    lines.extend(
        [
            "",
            "## Criteria",
            "- TARGET ≤ 16 µA; GOOD ≤ 25 µA; intermediate success ≤ 50 µA",
            "",
            "## Method",
            "1. Dedicated build per variant (`AETHER_DIAG_SLEEP_POWER_BISECT`)",
            "2. PPK power OFF → ON (3.0 V)",
            "3. Wait Espressif COM (VID 0x303A)",
            "4. Flash → reset → wait for deep sleep (~8 s flash window)",
            "5. Measure ≥30 s sleep current on same PPK session",
            "",
            "## Notes",
            "- STATUS_LED_ON (GPIO17) OFF polarity unverified; B1=LOW vs B2=HIGH.",
            "- PWR_ON (GPIO2) OFF=LOW (active-high rail enable).",
            "- Diag flash windows are compile-time only; not for production.",
            "",
        ]
    )
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


def git_meta() -> tuple[str, str]:
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    return branch, sha


def run_one(
    variant: str,
    ppk: PpkSession,
    measure_s: float,
    cold_wait_s: float,
    skip_build: bool,
    skip_flash: bool = False,
    result_name: str | None = None,
) -> dict:
    build_dir = ROOT / "build-sleep-bisect"
    notes: list[str] = []
    status = "OK"
    stored_name = result_name or variant

    if not skip_build:
        print(f"=== BUILD {variant} ===", flush=True)
        try:
            build_variant(variant, build_dir)
        except Exception as e:
            return {
                "name": stored_name,
                "status": "BUILD_FAIL",
                "notes": str(e),
                "build_dir": str(build_dir),
            }

    port = None
    if skip_flash:
        print(f"=== POWER CYCLE (no flash) {variant} ===", flush=True)
        ppk.power_cycle()
        port = wait_espressif_com(timeout_s=max(35.0, cold_wait_s))
        if not port:
            return {
                "name": stored_name,
                "status": "NO_COM",
                "notes": "Espressif COM not found after PPK power ON",
                "build_dir": str(build_dir),
            }
    else:
        print(f"=== POWER CYCLE + FLASH {variant} ===", flush=True)
        try:
            port = try_flash_with_retries(ppk, build_dir, cold_wait_s)
        except Exception as e:
            return {
                "name": stored_name,
                "status": "FLASH_FAIL" if "NO_COM" not in str(e) else "NO_COM",
                "notes": str(e),
                "build_dir": str(build_dir),
            }

    time.sleep(1.0)
    port2 = wait_espressif_com(timeout_s=20.0) or port
    print(f"=== RESET {variant} on {port2} ===", flush=True)
    try:
        hard_reset(port2)
    except Exception as e:
        notes.append(f"reset_warn={e}")

    # After flash/reset: firmware flash window ~8s then deep sleep.
    # Use 15s to clear the window with margin (cold 25s only on power-on).
    print("Waiting for deep sleep entry (~15s after reset)...", flush=True)
    time.sleep(15.0)

    csv_path = RESULTS / f"thermometer2_sleep_{stored_name}.csv"
    print(f"=== MEASURE {variant} {measure_s}s ===", flush=True)
    try:
        meas = ppk.measure(measure_s, csv_path)
        sleep_ua = meas.get("sleep_avg_uA", meas["median_uA"])
        sleep_frac = meas.get("sleep_frac", 0.0)
        if sleep_frac < 0.5 and meas["median_uA"] > 500:
            status = "NO_SLEEP"
            notes.append("low sleep_frac / high median")
        elif sleep_ua > 200:
            notes.append("elevated_sleep_or_leak")
        notes.append(
            f"sleep_avg_uA={sleep_ua:.2f} sleep_frac={sleep_frac:.3f}"
        )
    except Exception as e:
        return {
            "name": stored_name,
            "status": "MEASURE_FAIL",
            "notes": str(e),
            "port": port2,
            "build_dir": str(build_dir),
        }

    return {
        "name": stored_name,
        "status": status,
        "notes": "; ".join(notes),
        "port": port2,
        "build_dir": str(build_dir),
        "voltage_mv": ppk.voltage_mv,
        "measure_duration_s": measure_s,
        "measure": meas,
        "utc": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voltage-mv", type=int, default=3000)
    ap.add_argument("--measure-s", type=float, default=30.0)
    ap.add_argument("--cold-wait-s", type=float, default=40.0)
    ap.add_argument("--variants", nargs="*", default=None)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-flash", action="store_true")
    ap.add_argument("--no-md", action="store_true")
    ap.add_argument("--result-name", default=None)
    ap.add_argument("--confirmations", type=int, default=0)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    ensure_ppk_path()
    branch, sha = git_meta()
    data = load_results()
    data["branch"] = branch
    data["sha"] = sha
    data["voltage_mv"] = args.voltage_mv
    data["original_backup"] = "backup/thermometer2-before-sleep-power-bisect"
    data["original_sha"] = "4ee8bfcb4d086be9820433eeb4a26ddd2e9aa0bd"
    data["original_branch"] = "diag/deep-sleep-10min"

    variants = (
        [args.only]
        if args.only
        else (args.variants if args.variants else VARIANTS_DEFAULT)
    )

    ppk = PpkSession(args.voltage_mv)
    ppk.open()
    write_md_flag = not args.no_md
    try:
        for variant in variants:
            if args.confirmations > 0:
                # Build once, then N confirmation flashes/measures.
                if not args.skip_build:
                    print(f"=== BUILD {variant} (confirmations) ===", flush=True)
                    build_variant(variant, ROOT / "build-sleep-bisect")
                data.setdefault("confirmations", [])
                for i in range(1, args.confirmations + 1):
                    cname = f"CONFIRM_{i}_{variant}"
                    result = run_one(
                        variant,
                        ppk,
                        args.measure_s,
                        args.cold_wait_s,
                        skip_build=True,
                        skip_flash=args.skip_flash,
                        result_name=cname,
                    )
                    result["firmware_variant"] = variant
                    data["confirmations"] = [
                        c
                        for c in data.get("confirmations", [])
                        if c.get("name") != cname
                    ]
                    data["confirmations"].append(result)
                    save_results(data, write_markdown=write_md_flag)
                    avg = (result.get("measure") or {}).get("sleep_avg_uA")
                    print(
                        f"RESULT {cname} status={result.get('status')} "
                        f"sleep_avg_uA={avg}",
                        flush=True,
                    )
                continue

            stored = args.result_name or variant
            if not args.result_name:
                data["variants"] = [
                    v for v in data.get("variants", []) if v.get("name") != variant
                ]
            result = run_one(
                variant,
                ppk,
                args.measure_s,
                args.cold_wait_s,
                args.skip_build,
                skip_flash=args.skip_flash,
                result_name=stored,
            )
            if args.result_name:
                data.setdefault("confirmations", [])
                data["confirmations"] = [
                    c
                    for c in data.get("confirmations", [])
                    if c.get("name") != stored
                ]
                data["confirmations"].append(result)
            else:
                data["variants"].append(result)
            save_results(data, write_markdown=write_md_flag)
            avg = (result.get("measure") or {}).get("sleep_avg_uA")
            print(
                f"RESULT {stored} status={result.get('status')} sleep_avg_uA={avg}",
                flush=True,
            )
    finally:
        # Leave DUT powered for operator convenience.
        try:
            ppk.set_on()
        except Exception:
            pass
        ppk.close()

    save_results(data, write_markdown=write_md_flag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
