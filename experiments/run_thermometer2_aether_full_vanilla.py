#!/usr/bin/env python3
"""Thermometer 2: full Æther + vanilla Wi-Fi deep-sleep benchmark runner."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PREPARED_ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
sys.path.insert(0, str(HERE))
if str(PREPARED_ROOT / "experiments") not in sys.path:
    sys.path.append(str(PREPARED_ROOT / "experiments"))
BUILD = ROOT / "build-thermometer2-aether-full-vanilla"
RESULTS = HERE / "thermometer2_full_vanilla_results"
MEASURED = 100
SLEEP_S = 20
VOLTAGE_MV = 3000
VOLTAGE_V = VOLTAGE_MV / 1000.0
FS_HZ = 100_000.0
GUARD_SAMPLES = int(0.75 * FS_HZ)
COLD_BOOT_S = 60.0
RAW_DIR = RESULTS / "ppk_raw"
CSV_OUT = RESULTS / "thermometer2_full_vanilla_100.csv"
JSON_OUT = RESULTS / "thermometer2_full_vanilla_100.json"
MD_OUT = HERE / "FULL_VANILLA.md"

import run_thermometer2_sleep_bisect as sb  # noqa: E402
from ppk_stream_cycle_segmenter import (  # noqa: E402
    CloseReason,
    CycleWindow,
    StreamingCycleSegmenter,
    thermometer2_full_vanilla_segmenter_config,
)
RX_SRC = PREPARED_ROOT / "temperature_receiver"
RX_BUILD = HERE / "thermometer2_full_vanilla_rx_build"
AETHER_LOCAL = ROOT / "deps" / "aether-client-cpp-157aadbe-diag"
FS_INIT = HERE / "preprovision" / "sender_fs_157aadbe.h"
AETHER_PIN = "157aadbec8e7b852d0f89274307ff7cb8103e5f7"
BENCH_CLIENT_ID = "reliability_full_v1"

RE_RX_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_FULL_RX = re.compile(r"AETHER_FULL_READY run_id=(\d+)")
RE_RUN_ID = re.compile(r"run_id=(\d+)")
RE_JOURNAL = re.compile(
    r"JOURNAL cycle=(\d+) run_id=(\d+) got_ip=(\d+) select_ok=(\d+) "
    r"write_called=(\d+) write_status=(\d+) fail_stage=(\d+) active_ms=(\d+)"
)


def load_wifi() -> tuple[str, str]:
    for env_path in (
        HERE / "local_aether_wifi.env",
        PREPARED_ROOT / "experiments" / "local_aether_wifi.env",
    ):
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"")
            if k and v and k not in os.environ:
                os.environ[k] = v
        break
    ssid = os.environ.get("AETHER_WIFI_SSID", "").strip()
    pwd = os.environ.get("AETHER_WIFI_PASSWORD", "").strip()
    if not ssid or not pwd:
        raise RuntimeError("Set AETHER_WIFI_SSID/AETHER_WIFI_PASSWORD or local_aether_wifi.env")
    return ssid, pwd


def idf_ps(extra: str) -> list[str]:
    export = Path(r"C:\Espressif\frameworks\esp-idf-v6.0.2\export.ps1")
    ps = f"""
. '{export.as_posix()}'
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


def run_idf(extra: str) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(idf_ps(extra), cwd=str(ROOT), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"idf failed code={r.returncode}")


def build_esp(*, service_uid: str, ssid: str, password: str, smoke: bool, port: str, flash: bool) -> None:
    b = BUILD.as_posix()
    pwd = password.replace("'", "''")
    ssid_ps = ssid.replace("'", "''")
    fs = FS_INIT.as_posix() if FS_INIT.is_file() else ""
    aether = AETHER_LOCAL.as_posix() if AETHER_LOCAL.is_dir() else ""
    cycles = "3" if smoke else "100"
    stage = "1" if smoke else "0"
    sdk_defaults = (
        f"{ROOT.as_posix()}/sdkconfig.defaults;"
        f"{ROOT.as_posix()}/sdkconfig.defaults.thermometer2_aether_full_vanilla"
    )
    if smoke:
        sdk_defaults += f";{ROOT.as_posix()}/sdkconfig.defaults.bench"
    extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG_DEFAULTS={sdk_defaults}',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_THERMOMETER2_FULL_VANILLA=1',
  '-D','BOARD=0',
  '-D','AE_EXP_WIFI_CANONICAL=1',
  '-D','AE_EXP_BENCH_STAGE={stage}',
  '-D','AE_THERM_FULL_MEASURE_CYCLES={cycles}',
  '-D','AE_THERM_FULL_VANILLA_SLEEP_S=20',
  '-D','AE_THERM_FULL_ACTIVE_TIMEOUT_MS=60000',
  '-D','CPM_aether-client-cpp_SOURCE={aether}',
  '-D','USER_CONFIG={ROOT.as_posix()}/main/user_config_full_quiet.h'
)
"""
    if fs:
        extra += f"$common += @('-D','FS_INIT={fs}')\n"
    extra += f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
$env:CMAKE_BUILD_PARALLEL_LEVEL = '1'
$env:NINJA_STATUS = '[%f/%t] '
idf.py @common reconfigure build
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    if flash:
        extra += f"""
idf.py @common -p {port} flash
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    run_idf(extra)


def build_receiver() -> Path:
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    ps = f"""
$ErrorActionPreference='Stop'
$vswhere = "${{env:ProgramFiles(x86)}}\\Microsoft Visual Studio\\Installer\\vswhere.exe"
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvars = Join-Path $vs 'VC\\Auxiliary\\Build\\vcvars64.bat'
$rxs = '{RX_SRC.as_posix()}'
$rxb = '{RX_BUILD.as_posix()}'
$ae = '{AETHER_LOCAL.as_posix()}'
cmd /c "`"$vcvars`" && cmake -S %rxs% -B %rxb% -G Ninja -DCMAKE_BUILD_TYPE=Release -DCPM_aether-client-cpp_SOURCE=%ae% && cmake --build %rxb% --target aether_hot_tcp_receiver --parallel"
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError("receiver build failed")
    exe = RX_BUILD / "aether_hot_tcp_receiver.exe"
    if not exe.is_file():
        raise RuntimeError(f"missing {exe}")
    return exe


def start_receiver(exe: Path, session: Path) -> subprocess.Popen:
    session.mkdir(parents=True, exist_ok=True)
    for f in session.glob("*"):
        if f.is_file():
            f.unlink()
    env = os.environ.copy()
    env["AE_RECEIVER_SESSION_DIR"] = str(session)
    log = RESULTS / "receiver.log"
    RESULTS.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [str(exe), "--require-tcp"],
        stdout=open(log, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        env=env,
    )


def wait_uid(log: Path, timeout: float = 300.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.is_file():
            m = RE_RX_UID.search(log.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
        time.sleep(1.0)
    raise RuntimeError("receiver UID timeout")


def find_com() -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_SerialPort | Where-Object { $_.PNPDeviceID -match 'VID_303A' } | Select-Object -ExpandProperty DeviceID -First 1"],
        capture_output=True,
        text=True,
    )
    port = (r.stdout or "").strip()
    if not port:
        raise RuntimeError("No Espressif COM port")
    return port


def capture(port: str, seconds: int, out: Path) -> None:
    elf = BUILD / "temperature_sensor.elf"
    mon = f"python $env:IDF_PATH/tools/idf_monitor.py -p {port} -b 115200 {elf.as_posix()}"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.Popen(
        idf_ps(mon),
        stdout=open(out, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        env=env,
    )
    try:
        end = time.time() + seconds
        while time.time() < end:
            if out.is_file() and "CAMPAIGN_DONE" in out.read_text(encoding="utf-8", errors="replace"):
                time.sleep(3)
                break
            time.sleep(2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    return float(np.percentile(np.asarray(vals, dtype=float), p))


def agg_stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "p95": None, "min": None, "max": None}
    return {
        "n": len(vals),
        "mean": float(statistics.mean(vals)),
        "median": float(statistics.median(vals)),
        "p95": pct(vals, 95),
        "min": float(min(vals)),
        "max": float(max(vals)),
    }


def parse_journal(text: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for m in RE_JOURNAL.finditer(text):
        cycle = int(m.group(1))
        out[cycle] = {
            "run_id": int(m.group(2)),
            "got_ip": int(m.group(3)),
            "select_ok": int(m.group(4)),
            "write_called": int(m.group(5)),
            "write_status": int(m.group(6)),
            "fail_stage": int(m.group(7)),
            "active_ms": int(m.group(8)),
        }
    return out


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
    t, u = load_raw_slice(raw_bin, w.wake_start_sample, w.wake_end_sample)
    invalid_reason = ""
    valid = True
    if w.max_gap_samples >= 500:
        valid = False
        invalid_reason = "INVALID_ACQUISITION_GAP"
    if w.close_reason == CloseReason.FORCE_LIMIT:
        valid = False
        invalid_reason = invalid_reason or "force_limit_clip"
    elif w.close_reason == CloseReason.FINALIZE:
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
    return {
        "ppk_sequence": w.sequence,
        "duration_ms": duration_s * 1000.0,
        "charge_mC": charge_mC,
        "energy_mJ": charge_mC * VOLTAGE_V,
        "mean_current_mA": avg_mA,
        "peak_current_mA": peak_mA,
        "valid": valid,
        "invalid_reason": invalid_reason,
        "close_reason": str(w.close_reason.value),
    }


def measured_ppk_windows(windows: list[CycleWindow], n: int) -> list[CycleWindow]:
    ordered = sorted(windows, key=lambda w: w.wake_start_s)
    if len(ordered) >= n:
        return ordered[:n]
    return ordered


def build_measure_rows(
    windows: list[CycleWindow],
    raw_bin: Path,
    journal: dict[int, dict],
    rx_run_ids: set[int],
) -> list[dict]:
    ppk_w = measured_ppk_windows(windows, MEASURED)
    rows: list[dict] = []
    prev_end: float | None = None
    for i, w in enumerate(ppk_w, start=1):
        base = integrate_window(raw_bin, w)
        j = journal.get(i, {})
        run_id = int(j.get("run_id", 0))
        sleep_d = w.wake_start_s - prev_end if prev_end is not None else None
        sleep_charge = None
        if prev_end is not None and sleep_d and sleep_d > 0:
            t0 = int(prev_end * FS_HZ)
            t1 = int(w.wake_start_s * FS_HZ)
            ts, us = load_raw_slice(raw_bin, t0, t1)
            if len(ts) >= 2:
                try:
                    sleep_charge = abs(float(np.trapezoid(us, ts))) / 1000.0
                except AttributeError:
                    sleep_charge = abs(float(np.trapz(us, ts))) / 1000.0
        write_ok = int(j.get("write_status", 255)) == 0
        rx = run_id in rx_run_ids if run_id else False
        rows.append(
            {
                "cycle": i,
                "run_id": run_id,
                **base,
                "sleep_duration_s": sleep_d,
                "sleep_charge_mC": sleep_charge,
                "got_ip": j.get("got_ip"),
                "select_ok": j.get("select_ok"),
                "write_called": j.get("write_called"),
                "write_terminal_ok": write_ok,
                "fail_stage": j.get("fail_stage"),
                "receiver_received": rx,
                "active_ms_esp": j.get("active_ms"),
            }
        )
        prev_end = w.wake_end_s
    return rows


class ContinuousPpkCapture:
    def __init__(self, ppk: sb.PpkSession, raw_bin: Path, meta_path: Path) -> None:
        self.ppk = ppk
        self.raw_bin = raw_bin
        self.meta_path = meta_path
        self.bin_f = raw_bin.open("wb")
        self.cfg = thermometer2_full_vanilla_segmenter_config(cycles=MEASURED)
        self.segmenter = StreamingCycleSegmenter(self.cfg)
        self.sample_count = 0
        self.wall_start = 0.0

    def start(self) -> None:
        self.wall_start = time.time()
        self.ppk.ppk.start_measuring()

    def stop(self) -> None:
        self.segmenter.finalize()
        self.ppk.ppk.stop_measuring()
        self.bin_f.close()
        meta = {
            "fs_hz": FS_HZ,
            "wall_start_utc": datetime.fromtimestamp(self.wall_start, tz=timezone.utc).isoformat(),
            "voltage_mV": VOLTAGE_MV,
            "sample_count": self.sample_count,
            "max_wake_s": self.cfg.max_wake_s,
            "sleep_s": SLEEP_S,
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


def capture_measure_serial(port: str, seconds: int, out: Path) -> None:
    """Read compact JOURNAL lines after the 100-cycle campaign."""
    try:
        import serial  # type: ignore
    except ImportError:
        out.write_text("", encoding="utf-8")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = ""
    with serial.Serial(port, 115200, timeout=0.3) as ser, out.open(
        "w", encoding="utf-8", errors="replace"
    ) as f:
        end = time.time() + seconds
        while time.time() < end:
            data = ser.read(8192)
            if data:
                buf += data.decode("utf-8", errors="replace")
                f.write(data.decode("utf-8", errors="replace"))
                f.flush()
                if "CAMPAIGN_DONE" in buf and buf.count("JOURNAL_DUMP cycle=") >= MEASURED:
                    break
            time.sleep(0.05)


def parse_smoke(esp_log: Path, rx_log: Path) -> dict:
    esp = esp_log.read_text(encoding="utf-8", errors="replace") if esp_log.is_file() else ""
    rx = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.is_file() else ""
    run_ids = set(RE_RUN_ID.findall(esp))
    full_rx = set(RE_FULL_RX.findall(rx))
    return {
        "esp_run_ids": sorted(run_ids, key=int),
        "full_rx_run_ids": sorted(full_rx, key=int),
        "full_rx_count": len(full_rx),
        "panic": "Guru Meditation" in esp or "assert failed" in esp,
    }


def firmware_sha256() -> str:
    bin_path = BUILD / "temperature_sensor.bin"
    if not bin_path.is_file():
        return ""
    return hashlib.sha256(bin_path.read_bytes()).hexdigest()


def write_measure_artifacts(rows: list[dict], meta: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    fields = [
        "cycle",
        "run_id",
        "duration_ms",
        "charge_mC",
        "energy_mJ",
        "mean_current_mA",
        "peak_current_mA",
        "sleep_duration_s",
        "sleep_charge_mC",
        "got_ip",
        "select_ok",
        "write_called",
        "write_terminal_ok",
        "fail_stage",
        "receiver_received",
        "valid",
        "invalid_reason",
        "close_reason",
    ]
    with CSV_OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    JSON_OUT.write_text(json.dumps({"meta": meta, "cycles": rows}, indent=2), encoding="utf-8")
    rx_n = sum(1 for r in rows if r.get("receiver_received"))
    valid_ppk = sum(1 for r in rows if r.get("valid"))
    MD_OUT.write_text(
        "\n".join(
            [
                "# FULL vanilla Thermometer2 (stage 1)",
                "",
                f"- RX unique / {MEASURED}: {rx_n}/{MEASURED}",
                f"- PPK complete windows: {valid_ppk}/{MEASURED}",
                f"- Git: {meta.get('git_head')}",
                f"- Firmware SHA256: {meta.get('firmware_sha256')}",
                "",
                json.dumps(meta.get("wifi_config", {}), indent=2),
            ]
        ),
        encoding="utf-8",
    )


def run_measure(port: str, uid: str, ssid: str, pwd: str) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_bin = RAW_DIR / "full_vanilla_100.bin"
    meta_bin = raw_bin.with_suffix(".json")
    sb.stop_external_ppk_holders()
    ppk = sb.PpkSession(VOLTAGE_MV)
    ppk.open()
    rx_log = RESULTS / "receiver.log"
    try:
        build_esp(service_uid=uid, ssid=ssid, password=pwd, smoke=False, port=port, flash=True)
        fw_sha = firmware_sha256()
        cap = ContinuousPpkCapture(ppk, raw_bin, meta_bin)
        cap.start()
        ppk.power_cycle()
        deadline = time.time() + COLD_BOOT_S + MEASURED * (65.0 + SLEEP_S) + 120.0
        while time.time() < deadline:
            cap.drain()
            if len(cap.segmenter.windows_closed) >= MEASURED:
                time.sleep(30.0)
                cap.drain()
                break
            time.sleep(0.002)
        cap.stop()
        esp_log = RESULTS / "measure_esp.log"
        capture_measure_serial(port, 180, esp_log)
        esp_text = esp_log.read_text(encoding="utf-8", errors="replace") if esp_log.is_file() else ""
        journal = parse_journal(esp_text)
        rx_text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.is_file() else ""
        rx_ids = set(int(x) for x in RE_FULL_RX.findall(rx_text))
        rows = build_measure_rows(cap.segmenter.windows_closed, raw_bin, journal, rx_ids)
        git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
        meta = {
            "git_head": git_head,
            "firmware_sha256": fw_sha,
            "aether_pin": AETHER_PIN,
            "receiver_uid": uid,
            "ppk_windows": len(cap.segmenter.windows_closed),
            "journal_lines": len(journal),
            "wifi_config": {
                "AE_EXP_WIFI_CANONICAL": 1,
                "AE_EXP_WIFI_FEAT_*": "all unset/0",
                "WIFI_PS": "WIFI_PS_NONE",
                "DHCP": True,
                "sleep_s": SLEEP_S,
            },
            "PREPARED_USED": "no",
            "HOT_USED": "no",
            "WIFI_CACHE_USED": "no",
        }
        write_measure_artifacts(rows, meta)
        active_charge = [r["charge_mC"] for r in rows if r.get("valid")]
        print(json.dumps({"rx": sum(1 for r in rows if r.get("receiver_received")), "stats": agg_stats(active_charge)}, indent=2))
        return 0
    finally:
        ppk.set_off()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--measure", action="store_true", help="100-cycle PPK measure (after smoke)")
    ap.add_argument("--flash", action="store_true", default=True)
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "branch_head.json").write_text(
        json.dumps({"branch": "diag/thermometer2-aether-full-vanilla", "head": head}, indent=2),
        encoding="utf-8",
    )

    ssid, pwd = load_wifi()
    if args.build_only:
        build_esp(
            service_uid="00000000-0000-0000-0000-000000000001",
            ssid=ssid,
            password=pwd,
            smoke=args.smoke,
            port="COM5",
            flash=False,
        )
        print(f"FIRMWARE_SHA256={firmware_sha256()}", flush=True)
        return 0

    port = find_com()
    exe = build_receiver()
    rx_log = RESULTS / "receiver.log"
    session = HERE / "thermometer2_full_vanilla_rx_session"
    proc = start_receiver(exe, session)
    try:
        uid = wait_uid(rx_log)
        print(f"RECEIVER_UID={uid}", flush=True)
        if args.measure:
            return run_measure(port, uid, ssid, pwd)
        build_esp(service_uid=uid, ssid=ssid, password=pwd, smoke=args.smoke, port=port, flash=args.flash)
        print(f"FIRMWARE_SHA256={firmware_sha256()}", flush=True)
        esp_log = RESULTS / "smoke_esp.log"
        capture(port, 600 if args.smoke else 7200, esp_log)
        if args.smoke:
            res = parse_smoke(esp_log, rx_log)
            out = RESULTS / "smoke_result.json"
            out.write_text(json.dumps(res, indent=2), encoding="utf-8")
            print(json.dumps(res, indent=2), flush=True)
            ok = res["full_rx_count"] >= 3 and not res["panic"]
            return 0 if ok else 1
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
