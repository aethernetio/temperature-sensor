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
                raw = self.ppk.get_data()
                now = time.time()
                if raw:
                    chunk, _ = self.ppk.get_samples(raw)
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
            except Exception:
                pass

        with out_csv.open("w", encoding="utf-8", newline="") as f:
            f.write("t_s,uA\n")
            for t, u in samples:
                f.write(f"{t:.6f},{u:.3f}\n")

        use = [u for t, u in samples if t >= 2.0]
        if len(use) < 100:
            use = [u for _, u in samples]
        if not use:
            raise RuntimeError("no current samples")
        use_sorted = sorted(use)
        mid = len(use_sorted) // 2
        median = (
            use_sorted[mid]
            if len(use_sorted) % 2
            else 0.5 * (use_sorted[mid - 1] + use_sorted[mid])
        )
        return {
            "samples": len(use),
            "avg_uA": statistics.fmean(use),
            "median_uA": median,
            "min_uA": min(use),
            "max_uA": max(use),
            "stdev_uA": statistics.pstdev(use) if len(use) > 1 else 0.0,
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
    # Use export.ps1 from IDF v6 tree + idf6 python explicitly.
    ps = f"""
$ErrorActionPreference = 'Stop'
$env:IDF_PATH = '{IDF_PATH.as_posix()}'
$env:IDF_PYTHON = '{IDF_PYTHON.as_posix()}'
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
    # First configure if needed, then build. BOARD=0 => AETHER_ESP32_C6.
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
    b = build_dir.as_posix()
    extra = f"idf.py -B '{b}' -p {port} flash"
    r = run(idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"flash failed port={port} code={r.returncode}")


def hard_reset(port: str) -> None:
    ensure_ppk_path()
    import serial

    with serial.Serial(port, 115200, timeout=0.2) as ser:
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.1)
        ser.setRTS(False)
        time.sleep(0.05)


def load_results() -> dict:
    if JSON_OUT.exists():
        return json.loads(JSON_OUT.read_text(encoding="utf-8"))
    return {
        "repo": "temperature-sensor-prepared (origin aethernetio/temperature-sensor)",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "voltage_mv": 3000,
        "variants": [],
    }


def save_results(data: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    data["updated_utc"] = datetime.now(timezone.utc).isoformat()
    JSON_OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
        "| Variant | avg_uA | median_uA | min_uA | max_uA | duration_s | status | notes |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for v in data.get("variants", []):
        m = v.get("measure") or {}
        avg = m.get("avg_uA")
        med = m.get("median_uA")
        mn = m.get("min_uA")
        mx = m.get("max_uA")

        def fmt(x):
            return f"{x:.2f}" if isinstance(x, (int, float)) else ""

        lines.append(
            f"| {v.get('name')} | {fmt(avg)} | {fmt(med)} | {fmt(mn)} | {fmt(mx)} | "
            f"{v.get('measure_duration_s', '')} | {v.get('status', '')} | {v.get('notes', '')} |"
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
) -> dict:
    build_dir = ROOT / f"build-sleep-bisect-{variant.lower()}"
    notes: list[str] = []
    status = "OK"

    if not skip_build:
        print(f"=== BUILD {variant} ===", flush=True)
        try:
            build_variant(variant, build_dir)
        except Exception as e:
            return {
                "name": variant,
                "status": "BUILD_FAIL",
                "notes": str(e),
                "build_dir": str(build_dir),
            }

    print(f"=== POWER CYCLE {variant} ===", flush=True)
    ppk.power_cycle()
    port = wait_espressif_com(timeout_s=max(35.0, cold_wait_s))
    if not port:
        # Cold window may need longer; try one more cycle.
        ppk.power_cycle()
        port = wait_espressif_com(timeout_s=55.0)
    if not port:
        return {
            "name": variant,
            "status": "NO_COM",
            "notes": "Espressif COM not found after PPK power ON",
            "build_dir": str(build_dir),
        }

    print(f"=== FLASH {variant} on {port} ===", flush=True)
    try:
        flash_port(port, build_dir)
    except Exception as e:
        return {
            "name": variant,
            "status": "FLASH_FAIL",
            "notes": str(e),
            "port": port,
            "build_dir": str(build_dir),
        }

    time.sleep(1.0)
    port2 = wait_espressif_com(timeout_s=20.0) or port
    print(f"=== RESET {variant} on {port2} ===", flush=True)
    try:
        hard_reset(port2)
    except Exception as e:
        notes.append(f"reset_warn={e}")

    print("Waiting for deep sleep entry (~12s after reset)...", flush=True)
    time.sleep(12.0)

    csv_path = RESULTS / f"thermometer2_sleep_{variant}.csv"
    print(f"=== MEASURE {variant} {measure_s}s ===", flush=True)
    try:
        meas = ppk.measure(measure_s, csv_path)
        if meas["avg_uA"] > 5000:
            status = "NO_SLEEP"
            notes.append("avg>5mA — likely awake")
        elif meas["avg_uA"] > 200:
            notes.append("elevated_sleep_or_leak")
    except Exception as e:
        return {
            "name": variant,
            "status": "MEASURE_FAIL",
            "notes": str(e),
            "port": port2,
            "build_dir": str(build_dir),
        }

    return {
        "name": variant,
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
    try:
        for variant in variants:
            data["variants"] = [
                v for v in data.get("variants", []) if v.get("name") != variant
            ]
            result = run_one(
                variant, ppk, args.measure_s, args.cold_wait_s, args.skip_build
            )
            data["variants"].append(result)
            save_results(data)
            avg = (result.get("measure") or {}).get("avg_uA")
            print(
                f"RESULT {variant} status={result.get('status')} avg_uA={avg}",
                flush=True,
            )
    finally:
        # Leave DUT powered for operator convenience.
        try:
            ppk.set_on()
        except Exception:
            pass
        ppk.close()

    save_results(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
