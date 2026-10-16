#!/usr/bin/env python3
"""Historical prepared-message E2E from restore worktree (no PPK capture)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
AETHER_LOCAL = ROOT / "deps" / "aether-client-cpp-157aadbe-diag"
RX_ROOT = HERE / "prepared_message_receiver"
RX_BUILD = RX_ROOT / "build-msvc"
RX_SESSION = HERE / "prepared_message_rx_session_restore"
ESP_BUILD = ROOT / "build-esp32c6-save-bench-smoke"
ESP_LOG = HERE / "prepared_message_e2e_esp_restore.log"
RX_LOG = HERE / "prepared_message_e2e_receiver_restore.log"
UID_FILE = HERE / "prepared_message_receiver_uid_restore.txt"
RESULT_JSON = HERE / "prepared_message_e2e_restore_result.json"

ESPRESSIF_VID = 0x303A
BENCH_CLIENT_ID = "prepared_message_bench_v6"
CAPTURE_SEC = 900


def cmake_common_defs(
    service_uid: str,
    ssid: str,
    password: str,
    *,
    precon_skip: bool,
    wifi_diag: bool,
) -> list[str]:
    defs = [
        "-D",
        "SDKCONFIG_DEFAULTS=sdkconfig.defaults",
        "-D",
        f"WIFI_SSID={ssid}",
        "-D",
        f"WIFI_PASSWORD={password}",
        "-D",
        f"SERVICE_UID={service_uid}",
        "-D",
        "BENCH_CLIENT_ID=" + BENCH_CLIENT_ID,
        "-D",
        "AE_EXP_PREPARED_MESSAGE_E2E=1",
        "-D",
        "AE_EXP_SKIP_DTOR_SAVE=1",
        "-D",
        "AE_EXP_WIFI_LIFECYCLE=",
        "-D",
        "AE_EXP_FULL_CYCLES=",
        "-D",
        "AETHER_PREPARED_NONCE_RESERVE=10",
    ]
    if wifi_diag:
        defs += ["-D", "AE_EXP_DIAG=1", "-D", "AE_EXP_WIFI_DRV_DIAG=1"]
    if precon_skip:
        defs += ["-D", "AE_EXP_PRECON_SKIP=1"]
    if AETHER_LOCAL.is_dir():
        defs += [
            "-D",
            f"CPM_aether-client-cpp_SOURCE={AETHER_LOCAL.as_posix()}",
        ]
    return defs


def ps_common_array(defs: list[str]) -> str:
    parts = []
    for d in defs:
        if d.startswith("-D") and "=" in d and " " not in d:
            k, _, v = d.partition("=")
            parts.append(f"'{k}','{v}'")
        elif d == "-D":
            continue
        else:
            parts.append(repr(d))
    # rebuild as pairs for WIFI_PASSWORD env case handled separately in build_esp
    return ""


def write_build_snapshot(tag: str, precon_skip: bool, wifi_diag: bool) -> None:
    import hashlib
    import json

    bin_path = ESP_BUILD / "temperature_sensor.bin"
    elf_path = ESP_BUILD / "temperature_sensor.elf"
    sdk = ESP_BUILD / "sdkconfig"
    snap = HERE / f"build_snapshot_{tag}.json"
    lib_sha = ""
    lib_dirty = ""
    if AETHER_LOCAL.is_dir():
        r = subprocess.run(
            ["git", "-C", str(AETHER_LOCAL), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        lib_sha = (r.stdout or "").strip()
        r2 = subprocess.run(
            ["git", "-C", str(AETHER_LOCAL), "status", "--short"],
            capture_output=True,
            text=True,
        )
        lib_dirty = (r2.stdout or "").strip()
    r3 = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    r4 = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--stat"],
        capture_output=True,
        text=True,
    )
    fw_hash = ""
    if bin_path.is_file():
        fw_hash = hashlib.sha256(bin_path.read_bytes()).hexdigest()
    compile_probe = ""
    probe = ESP_BUILD / "compile_flags_probe.txt"
    if probe.is_file():
        compile_probe = probe.read_text(encoding="utf-8", errors="replace")[:4000]
    data = {
        "tag": tag,
        "git_head": (r3.stdout or "").strip(),
        "git_diff_stat": (r4.stdout or "").strip(),
        "firmware_sha256": fw_hash,
        "elf": str(elf_path) if elf_path.is_file() else "",
        "sdkconfig": str(sdk) if sdk.is_file() else "",
        "library_path": str(AETHER_LOCAL),
        "library_sha": lib_sha,
        "library_local_patch": lib_dirty,
        "cmake_precon_skip": precon_skip,
        "cmake_wifi_diag": wifi_diag,
        "compile_probe_excerpt": compile_probe,
    }
    snap.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps(data, indent=2), flush=True)


def parse_wifi_diag(esp_text: str) -> dict:
    drivers: list[str] = []
    create_errs: list[int] = []
    wifi_init_errs: list[int] = []
    precon_skip = "reason=AE_EXP_PRECON_SKIP" in esp_text
    precon_ran = "PRECON_CLEANUP run=1" in esp_text
    precon_delete = None
    precon_netif = None
    dtor_ends = 0
    for line in esp_text.splitlines():
        if line.startswith("WIFI_DRV_DIAG"):
            m = re.search(r"ptr=(0x[0-9a-fA-F]+)", line)
            if m and m.group(1) not in drivers:
                drivers.append(m.group(1))
            if "esp_event_loop_create_default" in line:
                em = re.search(r"err=(-?\d+)", line)
                if em:
                    create_errs.append(int(em.group(1)))
            if "esp_wifi_init" in line:
                em = re.search(r"err=(-?\d+)", line)
                if em:
                    wifi_init_errs.append(int(em.group(1)))
            if "\tdtor\t" in line and "end" in line:
                dtor_ends += 1
        if "LC\t" in line and "dtor_end" in line:
            dtor_ends += 1
        if "LC\t" in line and "Deinit_end" in line:
            dtor_ends += 1
        if "esp_event_loop_delete_default err=" in line:
            em = re.search(r"err=(-?\d+)", line)
            if em:
                precon_delete = int(em.group(1))
        if "esp_netif_deinit err=" in line:
            em = re.search(r"err=(-?\d+)", line)
            if em:
                precon_netif = int(em.group(1))
    return {
        "driver_ptrs": drivers,
        "first_driver_destroyed": dtor_ends >= 1,
        "precon_skipped": precon_skip,
        "precon_ran_historical": precon_ran,
        "precon_esp_netif_deinit_err": precon_netif,
        "precon_event_loop_delete_err": precon_delete,
        "second_create_default_err": create_errs[1] if len(create_errs) > 1 else None,
        "second_wifi_init_err": wifi_init_errs[1] if len(wifi_init_errs) > 1 else None,
        "create_default_errs": create_errs,
        "wifi_init_errs": wifi_init_errs,
    }


def extract_compile_flags_from_log() -> None:
    """After build, grep ninja log for main bench object compile line."""
    log_dir = ESP_BUILD / "log"
    probe = ESP_BUILD / "compile_flags_probe.txt"
    if not log_dir.is_dir():
        return
    logs = sorted(log_dir.glob("idf_py_stdout_output_*"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return
    text = logs[-1].read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if "prepared_message_e2e_bench.cpp.obj" in line and "-DAE_" in line:
            probe.write_text(line[-3500:], encoding="utf-8")
            return
        if "_deps/aether-client-cpp-build" in line and "esp_wifi_driver.cpp.obj" in line:
            probe.write_text("aether:" + line[-2000:], encoding="utf-8")
            return


def load_wifi() -> tuple[str, str]:
    for path in (
        HERE / "local_aether_wifi.env",
        ROOT.parent / "temperature-sensor-prepared/experiments/local_aether_wifi.env",
    ):
        if not path.is_file():
            continue
        env: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
        ssid = env.get("AETHER_WIFI_SSID", "")
        pwd = env.get("AETHER_WIFI_PASSWORD", "")
        if ssid and pwd:
            return ssid, pwd
    ssid = os.environ.get("AETHER_WIFI_SSID", "").strip()
    pwd = os.environ.get("AETHER_WIFI_PASSWORD", "").strip()
    if ssid and pwd:
        return ssid, pwd
    raise RuntimeError("Set AETHER_WIFI_SSID/PASSWORD or local_aether_wifi.env")


def find_espressif_com() -> str:
    try:
        import serial.tools.list_ports  # type: ignore
    except ImportError:
        raise RuntimeError("pip install pyserial") from None
    for p in serial.tools.list_ports.comports():
        if p.vid == ESPRESSIF_VID and p.device:
            return p.device
    raise RuntimeError("No Espressif COM (VID 0x303A)")


IDF_PATH = Path(os.environ.get("IDF_PATH", r"C:\Espressif\frameworks\esp-idf-v6.0.2"))
EXPORT_PS1 = IDF_PATH / "export.ps1"


def idf_ps(extra: str, cwd: Path | None = None) -> list[str]:
    root = (cwd or ROOT).as_posix()
    ps = f"""
$ErrorActionPreference = 'Stop'
$env:IDF_PATH = '{IDF_PATH.as_posix()}'
$env:PYTHONPATH = $null
$env:ESP_IDF_EXPORT_DEBUG = $null
. '{EXPORT_PS1.as_posix()}'
Set-Location '{root}'
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


def run_ps(script: str, cwd: Path) -> None:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        cwd=str(cwd),
        env=os.environ.copy(),
    )
    if r.returncode != 0:
        raise RuntimeError(f"powershell failed code={r.returncode}")


def release_serial_port(port: str) -> None:
    ps = f"""
$port = '{port}'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object {{ $_.CommandLine -match 'idf_monitor' -and $_.CommandLine -match [regex]::Escape($port) }} |
  ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Seconds 1
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=False,
    )


def run_idf(extra: str, cwd: Path | None = None) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(idf_ps(extra, cwd), cwd=str(cwd or ROOT), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"idf step failed code={r.returncode}")


def build_receiver() -> Path:
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    RX_SESSION.mkdir(parents=True, exist_ok=True)
    aether = (
        f"-DCPM_aether-client-cpp_SOURCE={AETHER_LOCAL.as_posix()}"
        if AETHER_LOCAL.is_dir()
        else ""
    )
    rx_s = RX_ROOT.as_posix()
    rx_b = RX_BUILD.as_posix()
    ps = f"""
$ErrorActionPreference='Stop'
$vswhere = "${{env:ProgramFiles(x86)}}\\Microsoft Visual Studio\\Installer\\vswhere.exe"
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvars = Join-Path $vs 'VC\\Auxiliary\\Build\\vcvars64.bat'
cmd /c "`"$vcvars`" && cmake -S {rx_s} -B {rx_b} -G Ninja -DCMAKE_BUILD_TYPE=Release {aether} && cmake --build {rx_b} --target prepared_message_receiver --parallel"
"""
    run_ps(ps, ROOT)
    exe = RX_BUILD / "prepared_message_receiver.exe"
    if not exe.is_file():
        raise RuntimeError(f"missing {exe}")
    return exe


def start_receiver(exe: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["AE_RECEIVER_SESSION_DIR"] = str(RX_SESSION)
    if RX_SESSION.exists():
        for f in RX_SESSION.glob("*"):
            if f.is_file():
                f.unlink()
    out = open(RX_LOG, "w", encoding="utf-8", errors="replace")
    err = open(str(RX_LOG) + ".err", "w", encoding="utf-8", errors="replace")
    return subprocess.Popen([str(exe)], stdout=out, stderr=err, env=env)


def wait_receiver_uid(timeout_s: float = 600.0) -> str:
    deadline = time.time() + timeout_s
    pat = re.compile(r"RECEIVER_UID=([0-9a-fA-F\-]{36})")
    while time.time() < deadline:
        if RX_LOG.is_file():
            m = pat.search(RX_LOG.read_text(encoding="utf-8", errors="replace"))
            if m:
                uid = m.group(1)
                UID_FILE.write_text(uid, encoding="utf-8")
                return uid
        time.sleep(2.0)
    raise RuntimeError("receiver UID timeout")


def build_esp(
    service_uid: str,
    ssid: str,
    password: str,
    flash: bool,
    port: str,
    *,
    precon_skip: bool,
    wifi_diag: bool,
) -> None:
    ESP_BUILD.mkdir(parents=True, exist_ok=True)
    import shutil

    cache = ESP_BUILD / "CMakeCache.txt"
    if ESP_BUILD.is_dir() and not cache.is_file():
        shutil.rmtree(ESP_BUILD)
    elif cache.is_file():
        text = cache.read_text(encoding="utf-8", errors="replace")
        if "IDF_TARGET:STRING=esp32c6" not in text:
            shutil.rmtree(ESP_BUILD)
    ESP_BUILD.mkdir(parents=True, exist_ok=True)
    b = ESP_BUILD.as_posix()
    pwd_ps = password.replace("'", "''")
    ssid_ps = ssid.replace("'", "''")
    extra_items: list[str] = []
    if wifi_diag:
        extra_items += ["AE_EXP_DIAG=1", "AE_EXP_WIFI_DRV_DIAG=1"]
    extra_items.append(f"AE_EXP_PRECON_SKIP={1 if precon_skip else 0}")
    if AETHER_LOCAL.is_dir():
        extra_items.append(f"CPM_aether-client-cpp_SOURCE={AETHER_LOCAL.as_posix()}")
    extra_ps = "\n".join(f"  '-D','{item}'," for item in extra_items)
    if extra_ps:
        # PowerShell rejects a trailing comma before ')' with no next element.
        extra_ps = extra_ps.rstrip(",\n") + "\n"
    extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd_ps}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG_DEFAULTS=sdkconfig.defaults',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_PREPARED_MESSAGE_E2E=1',
  '-D','AE_EXP_SKIP_DTOR_SAVE=1',
  '-D','AE_EXP_WIFI_LIFECYCLE=',
  '-D','AE_EXP_FULL_CYCLES=',
  '-D','AETHER_PREPARED_NONCE_RESERVE=10',
{extra_ps}
)
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py @common reconfigure
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
idf.py @common build
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    run_idf(extra)
    extract_compile_flags_from_log()
    if not flash:
        return
    release_serial_port(port)
    flash_extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd_ps}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG_DEFAULTS=sdkconfig.defaults',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_PREPARED_MESSAGE_E2E=1',
  '-D','AE_EXP_SKIP_DTOR_SAVE=1',
  '-D','AE_EXP_WIFI_LIFECYCLE=',
  '-D','AE_EXP_FULL_CYCLES=',
  '-D','AETHER_PREPARED_NONCE_RESERVE=10',
{extra_ps}
)
idf.py @common -p {port} erase-flash
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
idf.py @common -p {port} flash
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
"""
    run_idf(flash_extra)


def capture_serial(port: str, seconds: int) -> None:
    elf = (ESP_BUILD / "temperature_sensor.elf").as_posix()
    mon_extra = f"""
python $env:IDF_PATH/tools/idf_monitor.py -p {port} -b 115200 --print_filter *:I {elf}
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    mon = subprocess.Popen(
        idf_ps(mon_extra),
        stdout=open(ESP_LOG, "w", encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        env=env,
    )
    try:
        end = time.time() + seconds
        while time.time() < end:
            if "PREPARED_E2E_DONE" in ESP_LOG.read_text(encoding="utf-8", errors="replace"):
                time.sleep(5)
                break
            time.sleep(3)
    finally:
        mon.terminate()
        try:
            mon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mon.kill()


def parse_results() -> dict:
    rx_text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.is_file() else ""
    esp_text = ESP_LOG.read_text(encoding="utf-8", errors="replace") if ESP_LOG.is_file() else ""
    full_m = re.search(r"full=(\d+)/1", rx_text)
    prep_m = re.search(r"prepared=(\d+)/10", rx_text)
    dup_m = re.search(r"duplicates=(\d+)", rx_text)
    heap_panic = "assert failed" in esp_text or "Guru Meditation" in esp_text
    recv_full = len(re.findall(r"type=FULL", rx_text))
    recv_prep = len(re.findall(r"type=PREPARED", rx_text))
    res = {
        "full_rx": int(full_m.group(1)) if full_m else 0,
        "prepared_rx": int(prep_m.group(1)) if prep_m else 0,
        "duplicates": int(dup_m.group(1)) if dup_m else -1,
        "heap_panic": heap_panic,
        "esp_done": "PREPARED_E2E_DONE" in esp_text,
        "receiver_uid": UID_FILE.read_text(encoding="utf-8").strip() if UID_FILE.is_file() else "",
        "recv_full_lines": recv_full,
        "recv_prepared_lines": recv_prep,
    }
    res.update(parse_wifi_diag(esp_text))
    return res


def run_e2e(
    *,
    run_id: str,
    precon_skip: bool,
    wifi_diag: bool,
    rebuild: bool,
    erase_flash: bool,
) -> dict:
    global ESP_LOG, RX_LOG, RESULT_JSON, RX_SESSION
    ESP_LOG = HERE / f"prepared_message_e2e_esp_{run_id}.log"
    RX_LOG = HERE / f"prepared_message_e2e_receiver_{run_id}.log"
    RESULT_JSON = HERE / f"prepared_message_e2e_result_{run_id}.json"
    RX_SESSION = HERE / f"prepared_message_rx_session_{run_id}"

    ssid, password = load_wifi()
    port = find_espressif_com()
    print(f"RUN_ID={run_id} ESP_PORT={port} PRECON_SKIP={precon_skip}", flush=True)
    exe = build_receiver()
    proc = start_receiver(exe)
    try:
        uid = wait_receiver_uid()
        print(f"SERVICE_UID={uid}", flush=True)
        if rebuild:
            build_esp(
                uid,
                ssid,
                password,
                flash=True,
                port=port,
                precon_skip=precon_skip,
                wifi_diag=wifi_diag,
            )
        else:
            release_serial_port(port)
            b = ESP_BUILD.as_posix()
            if erase_flash:
                flash_only = f"""
idf.py -B '{b}' -p {port} erase-flash
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
idf.py -B '{b}' -p {port} flash
"""
            else:
                flash_only = f"idf.py -B '{b}' -p {port} flash"
            run_idf(flash_only)
        capture_serial(port, CAPTURE_SEC)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    import json

    res = parse_results()
    res["run_id"] = run_id
    res["precon_skip_build"] = precon_skip
    res["pass"] = (
        res["full_rx"] == 1
        and res["prepared_rx"] >= 8
        and not res["heap_panic"]
        and res["esp_done"]
    )
    RESULT_JSON.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2), flush=True)
    return res


def main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="restore")
    ap.add_argument("--precon-skip", type=int, default=-1, help="1=skip cleanup, 0=historical")
    ap.add_argument("--wifi-diag", type=int, default=1)
    ap.add_argument("--rebuild", type=int, default=1)
    ap.add_argument("--erase-flash", type=int, default=1)
    ap.add_argument("--campaign", action="store_true")
    args = ap.parse_args()

    if args.campaign:
        # 1) Diagnose with PreConstructCleanup skip (reproduces broken path)
        r_diag = run_e2e(
            run_id="diag_precon_skip",
            precon_skip=True,
            wifi_diag=True,
            rebuild=True,
            erase_flash=True,
        )
        write_build_snapshot("diag_precon_skip", True, True)
        hyp = "UNRESOLVED"
        sc = r_diag.get("second_create_default_err")
        if r_diag.get("precon_skipped") and sc == 259:  # ESP_ERR_INVALID_STATE
            hyp = "CONFIRMED"
        elif r_diag.get("precon_skipped") and sc == 0:
            hyp = "REJECTED"
        # 2) Historical PreConstructCleanup (fix build)
        r_fix1 = run_e2e(
            run_id="fix_historical_precon_1",
            precon_skip=False,
            wifi_diag=True,
            rebuild=True,
            erase_flash=True,
        )
        write_build_snapshot("fix_historical_precon", False, True)
        fw = (ESP_BUILD / "temperature_sensor.bin").read_bytes()
        import hashlib
        import shutil

        fw_hash = hashlib.sha256(fw).hexdigest()
        session_backup = HERE / "prepared_message_rx_session_fix1_backup"
        if RX_SESSION.exists():
            if session_backup.exists():
                shutil.rmtree(session_backup)
            shutil.copytree(RX_SESSION, session_backup)
        # 3) Repeat same binary (restore receiver session → same RECEIVER_UID)
        if session_backup.is_dir():
            if RX_SESSION.exists():
                shutil.rmtree(RX_SESSION)
            shutil.copytree(session_backup, RX_SESSION)
        r_fix2 = run_e2e(
            run_id="fix_historical_precon_2",
            precon_skip=False,
            wifi_diag=True,
            rebuild=False,
            erase_flash=True,
        )
        report = {
            "hypothesis": hyp,
            "diag": r_diag,
            "fix1": r_fix1,
            "fix2": r_fix2,
            "firmware_sha256": fw_hash,
        }
        (HERE / "wifi_e2e_campaign_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        ok = r_fix1.get("pass") and r_fix2.get("pass")
        return 0 if ok else 1

    precon = args.precon_skip == 1 if args.precon_skip >= 0 else False
    res = run_e2e(
        run_id=args.run_id,
        precon_skip=precon,
        wifi_diag=args.wifi_diag == 1,
        rebuild=args.rebuild == 1,
        erase_flash=args.erase_flash == 1,
    )
    write_build_snapshot(args.run_id, precon, args.wifi_diag == 1)
    return 0 if res.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
