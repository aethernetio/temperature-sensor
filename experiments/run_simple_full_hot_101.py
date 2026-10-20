#!/usr/bin/env python3
"""One run: 1× FULL vanilla + N× HOT (2 s sleep) with PPK2 power capture."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PREPARED = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
BUILD = ROOT / "build-simple-full-hot-101"
RESULTS = HERE / "simple_full_hot_101_results"
AETHER = ROOT / "deps" / "aether-client-cpp-157aadbe-diag"
AETHER_PATCH = HERE / "patches" / "aether-157aadbe-full-vanilla-stage1.patch"
USER_CONFIG = ROOT / "main" / "user_config_simple_full_hot_fsinit.h"
FS_INIT_HEADER = HERE / "preprovision" / "sender_fs_157aadbe.h"
RX_SRC = PREPARED / "temperature_receiver"
RX_BUILD = HERE / "simple_full_hot_101_rx_build"
BENCH_CLIENT_ID = "reliability_full_v1"
VOLTAGE_MV = 3000
USB_STARTUP_TIMEOUT_S = 90.0
BOOTSTRAP_EXTRA_TIMEOUT_S = 180.0
DIAG_LOCAL = RESULTS / "diag_local"
FULL_ACTIVE_TIMEOUT_S = 300.0
# Vanilla HOT wake: 2 s sleep + Wi-Fi + 1 s hold often exceeds 52 s wall time.
HOT_CYCLE_TIMEOUT_S = 120.0
TAIL_DELIVERY_S = 30.0
HOT_COUNT = 100
SLEEP_S = 2
LAST_MESSAGE = 1 + HOT_COUNT
_VENV_PY = PREPARED / "experiments" / "ppk2-venv" / "Scripts" / "python.exe"
if _VENV_PY.is_file() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PREPARED / "experiments"))
import run_thermometer2_sleep_bisect as sb  # noqa: E402
import run_thermometer2_aether_full_vanilla as vanilla  # noqa: E402
from simple_full_hot_101_parse import (  # noqa: E402
    extract_rtc_dumps,
    merge_rtc_into_fw_parse,
    pick_authoritative_rtc_dump,
)
from simple_full_hot_101_serial import SerialCapture  # noqa: E402
from simple_full_hot_101_campaign_timers import (  # noqa: E402
    CampaignTimerConfig,
    CampaignTimerState,
    SystemMonotonicClock,
    evaluate_deadlines,
    smoke_overall_deadline_s,
)
from simple_full_hot_101_ppk_capture import (  # noqa: E402
    SimpleFullHotPpkCapture,
    analyze_power_capture,
    classify_power_capture_status,
    sync_events_from_serial,
)
RE_UID = re.compile(r"RECEIVER_UID=([0-9a-f-]{36})", re.I)
RE_MSG = re.compile(
    r"MSG_NUM=(\d+)\s+sender=([0-9a-f-]{36})", re.I
)
RE_BOOT = re.compile(r"BOOT reset_reason=\d+ next_message=(\d+)", re.I)
RE_FULL_BEGIN = re.compile(r"FULL_BEGIN service_uid=([0-9a-f-]{36})", re.I)
RE_SELECT = re.compile(
    r"SELECT_RESULT ok=(\d+) error=(-?\d+) sender_uid=([0-9a-f-]{36})?", re.I
)
RE_SCENARIO_FAIL = re.compile(
    r"SCENARIO_FAIL stage=(\S+) error=(.*)", re.I
)
RE_HOT_BEGIN = re.compile(r"HOT_BEGIN number=(\d+)", re.I)
RE_FULL_WRITE = re.compile(r"FULL_WRITE_(?:BEGIN|STATUS)", re.I)
RE_FULL_WRITE_STATUS = re.compile(r"FULL_WRITE_STATUS=(\S+)", re.I)
RE_RX_TCP = re.compile(r"RX_HEALTH .* tcp_up=(\d+)")
RE_SELECT_FAIL = re.compile(
    r"(?:SELECT_RESULT|BOOTSTRAP_SELECT) ok=0 error=(\d+)", re.I
)
RE_HOT_SENDTO = re.compile(r"HOT_RESULT .* sendto_ok=(\d+)", re.I)
RE_WIFI_ESP = re.compile(r"WIFI_ESP call=(\S+) err=(-?\d+) name=(\S+)", re.I)
RE_FIRST_WIFI_ESP = re.compile(
    r"FIRST_WIFI_ESP call=(\S+) err=(-?\d+) name=(\S+)", re.I
)
RE_WIFI_RADIO = re.compile(r"WIFI_RADIO (\S+)", re.I)
RE_FULL_WIFI_PSP = re.compile(r"FULL_WIFI_PSP_PRESENT=(\d+)", re.I)
RE_GOT_IP = re.compile(r"^GOT_IP=1$")
RE_BOOTSTRAP_SELECT_FAIL = re.compile(
    r"BOOTSTRAP_SELECT ok=0 error=(\d+)", re.I
)
RE_HOT_WIFI = re.compile(r"HOT_WIFI=(\S+)", re.I)
RE_HOT_RTC_ROW = re.compile(
    r"HOT_RTC_ROW n=(\d+) got_ip=(\d+) "
    r"(?:encode=(\d+)|encode_called=(\d+) encode_ok=(\d+)) "
    r"nonce=(\d+) (?:sendto=(\d+)|sendto_called=(\d+) sendto=(\d+)) "
    r"err=(\d+) state=(\S+)",
    re.I,
)
def utc_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
def git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True
    ).strip()
def git_status_porcelain() -> str:
    return subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=str(ROOT), text=True
    )
class ResultWriter:
    def __init__(self, path: Path, latest: Path) -> None:
        self.path = path
        self.latest = latest
        self._lock = threading.Lock()
    def write(self, payload: dict) -> None:
        payload = dict(payload)
        payload["updated_at"] = iso_now()
        body = json.dumps(payload, indent=2)
        with self._lock:
            RESULTS.mkdir(parents=True, exist_ok=True)
            last_err: OSError | None = None
            for attempt in range(8):
                tmp = self.path.with_name(
                    f"{self.path.stem}.tmp.{attempt}.{os.getpid()}.json"
                )
                try:
                    tmp.write_text(body, encoding="utf-8")
                    tmp.replace(self.path)
                    last_err = None
                    break
                except OSError as e:
                    last_err = e
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    time.sleep(0.15 * (attempt + 1))
            if last_err is not None:
                self.path.write_text(body, encoding="utf-8")
            self.latest.write_text(
                json.dumps(
                    {
                        "latest_run_id": payload.get("run_id"),
                        "result": str(self.path.relative_to(ROOT)),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
def parse_firmware_serial(text: str) -> dict:
    out: dict = {
        "last_firmware_marker": None,
        "confirmed_failure_stage": None,
        "firmware_service_uid": None,
        "expected_sender_uid": None,
        "full_begin_count": 0,
        "full_write_called": 0,
        "full_write_statuses": [],
        "hot_begin_count": 0,
        "hot_sendto_ok_count": 0,
        "max_hot_begin": 0,
        "bootstrap_done": False,
        "bootstrap_select_fail": False,
        "client_reload_check": False,
        "scenario_done": False,
        "full_wifi_psp_present": None,
        "wifi_radio_applied": [],
        "got_ip": False,
        "first_wifi_esp_error": None,
        "wifi_esp_calls": [],
        "hot_wifi_mode": None,
        "hot_rtc_rows": {},
        "hot_rtc_table_complete": False,
    }
    last_line = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("SERIAL_"):
            continue
        last_line = line
        m = RE_FULL_BEGIN.search(line)
        if m:
            out["firmware_service_uid"] = m.group(1).lower()
            out["full_begin_count"] += 1
        if line.startswith("FULL_WRITE_BEGIN"):
            out["full_write_called"] += 1
        if line == "BOOTSTRAP_DONE":
            out["bootstrap_done"] = True
        if line.startswith("BOOTSTRAP_WRITE_STATUS=Success"):
            out["bootstrap_done"] = True
        m = re.search(r"BOOTSTRAP_SELECT ok=1 sender_uid=([0-9a-f-]{36})", line, re.I)
        if m:
            out["expected_sender_uid"] = m.group(1).lower()
            out["bootstrap_select_ok"] = True
        if RE_BOOTSTRAP_SELECT_FAIL.search(line):
            out["bootstrap_select_fail"] = True
        m = RE_FULL_WIFI_PSP.search(line)
        if m:
            out["full_wifi_psp_present"] = int(m.group(1))
        m = RE_WIFI_RADIO.search(line)
        if m:
            out["wifi_radio_applied"].append(m.group(1))
        if RE_GOT_IP.search(line):
            out["got_ip"] = True
        m = RE_FIRST_WIFI_ESP.search(line)
        if m and out["first_wifi_esp_error"] is None:
            out["first_wifi_esp_error"] = {
                "call": m.group(1),
                "err": int(m.group(2)),
                "name": m.group(3),
            }
        m = RE_WIFI_ESP.search(line)
        if m:
            out["wifi_esp_calls"].append(
                {"call": m.group(1), "err": int(m.group(2)), "name": m.group(3)}
            )
        m = RE_HOT_WIFI.search(line)
        if m:
            out["hot_wifi_mode"] = m.group(1).lower()
        m = RE_HOT_RTC_ROW.search(line)
        if m:
            n = int(m.group(1))
            if m.group(4) is not None:
                row = {
                    "got_ip": int(m.group(2)),
                    "encode_called": int(m.group(4)),
                    "encode_ok": int(m.group(5)),
                    "encode": int(m.group(5)),
                    "nonce": int(m.group(6)),
                    "sendto_called": int(m.group(8)),
                    "sendto": int(m.group(9)),
                    "err": int(m.group(10)),
                    "state": m.group(11).upper(),
                }
            else:
                enc = int(m.group(3))
                snd = int(m.group(7))
                row = {
                    "got_ip": int(m.group(2)),
                    "encode": enc,
                    "encode_called": enc,
                    "encode_ok": enc,
                    "nonce": int(m.group(6)),
                    "sendto": snd,
                    "sendto_called": snd,
                    "err": int(m.group(10)),
                    "state": m.group(11).upper(),
                }
            out["hot_rtc_rows"][n] = row
        if line.startswith("CLIENT_RELOAD_CHECK"):
            out["client_reload_check"] = True
        m = RE_SELECT_FAIL.search(line)
        if m and int(m.group(1)) == 3:
            out["confirmed_failure_stage"] = "select_client_load_failed"
        m = RE_SELECT.search(line)
        if m and m.group(1) == "1" and m.group(3):
            out["expected_sender_uid"] = m.group(3).lower()
        if RE_HOT_BEGIN.search(line):
            out["hot_begin_count"] += 1
            out["max_hot_begin"] = max(
                out["max_hot_begin"], int(RE_HOT_BEGIN.search(line).group(1))
            )
        m = RE_FULL_WRITE_STATUS.search(line)
        if m:
            out["full_write_statuses"].append(m.group(1))
        m = RE_HOT_SENDTO.search(line)
        if m and int(m.group(1)) == 1:
            out["hot_sendto_ok_count"] += 1
        if line == "SCENARIO_DONE":
            out["scenario_done"] = True
        m = RE_SCENARIO_FAIL.search(line)
        if m:
            out["confirmed_failure_stage"] = m.group(1)
            out["fail_error"] = m.group(2).strip()
    out["last_firmware_marker"] = last_line
    return merge_rtc_into_fw_parse(out, text, hot_count=HOT_COUNT)
def parse_receiver_meta(text: str) -> dict:
    tcp_up = 0
    for m in RE_RX_TCP.finditer(text):
        tcp_up = max(tcp_up, int(m.group(1)))
    uid_m = RE_UID.search(text)
    return {
        "receiver_uid": uid_m.group(1).lower() if uid_m else None,
        "receiver_tcp_up": bool(tcp_up),
    }
def stop_experiment_holders() -> None:
    sb.stop_external_ppk_holders()
    if sys.platform != "win32":
        return
    ps = r"""
Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -and (
      $_.CommandLine -match 'ppk2_hold_power|ppk2_log_power|ppk2_integrate_run|ppk2_power\.py' -or
      ($_.CommandLine -match 'idf_monitor|esp_idf_monitor|esptool' -and
       $_.CommandLine -match 'build-simple-full-hot-101')
    )
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        check=False,
    )
    time.sleep(1.0)
def recover_esp_port(ppk: sb.PpkSession) -> str | None:
    for i in range(3):
        ppk.set_off()
        time.sleep(4.0)
        ppk.set_on()
        port = sb.wait_espressif_com(timeout_s=60.0)
        if port:
            stop_experiment_holders()
            time.sleep(0.8)
            chip = vanilla.verify_esptool_chip(port)
            if chip.get("ok"):
                return port
        time.sleep(2.0)
    return None
def build_receiver() -> tuple[Path, str]:
    RX_BUILD.mkdir(parents=True, exist_ok=True)
    stop_experiment_holders()
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process aether_hot_tcp_receiver -ErrorAction SilentlyContinue | Stop-Process -Force",
            ],
            capture_output=True,
            check=False,
        )
        time.sleep(1.5)
    exe = RX_BUILD / "aether_hot_tcp_receiver.exe"
    ps = (
        "$ErrorActionPreference='Stop'\n"
        '$vswhere = "${env:ProgramFiles(x86)}\\Microsoft Visual Studio\\Installer\\vswhere.exe"\n'
        "$vs = & $vswhere -latest -products * -requires "
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath\n"
        "$vcvars = Join-Path $vs 'VC\\Auxiliary\\Build\\vcvars64.bat'\n"
        f'cmd /c "`"$vcvars`" && cmake -S `"{RX_SRC.as_posix()}`" -B '
        f'`"{RX_BUILD.as_posix()}`" -G Ninja -DCMAKE_BUILD_TYPE=Release '
        f"-DCPM_aether-client-cpp_SOURCE=`\"{AETHER.as_posix()}`\" && cmake --build "
        f'`"{RX_BUILD.as_posix()}`" --target aether_hot_tcp_receiver --parallel"\n'
    )
    subprocess.check_call(["powershell", "-NoProfile", "-Command", ps], cwd=str(ROOT))
    if not exe.is_file():
        exe = RX_BUILD / "Release" / "aether_hot_tcp_receiver.exe"
    return exe, file_sha256(exe)
def start_receiver(exe: Path, log_path: Path, session: Path) -> subprocess.Popen:
    session.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [str(exe), "--session", str(session), "--require-tcp", "1"],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(RX_BUILD),
    )
def wait_uid(log_path: Path, timeout_s: float = 90.0) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if log_path.is_file():
            m = RE_UID.search(log_path.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
        time.sleep(0.5)
    raise RuntimeError("RECEIVER_UID timeout")
def build_esp(*, service_uid: str, ssid: str, pwd: str) -> str:
    b = BUILD.as_posix()
    sdk = (
        f"{ROOT.as_posix()}/sdkconfig.defaults;"
        f"{ROOT.as_posix()}/sdkconfig.defaults.thermometer2_aether_full_vanilla;"
        f"{ROOT.as_posix()}/sdkconfig.defaults.simple_full_hot_101"
    )
    sdkconfig = f"{b}/sdkconfig"
    ssid_ps = ssid.replace("'", "''")
    pwd_ps = pwd.replace("'", "''")
    aether = AETHER.as_posix()
    ucfg = USER_CONFIG.as_posix()
    fs_init = FS_INIT_HEADER.as_posix()
    if not FS_INIT_HEADER.is_file():
        raise RuntimeError(f"missing FS_INIT header: {FS_INIT_HEADER}")
    extra = f"""
$env:AETHER_WIFI_PASSWORD = '{pwd_ps}'
$env:SDKCONFIG = '{sdkconfig}'
$common = @(
  '-B','{b}',
  '-D','SDKCONFIG={sdkconfig}',
  '-D','SDKCONFIG_DEFAULTS={sdk}',
  '-D',"WIFI_SSID={ssid_ps}",
  '-D',"WIFI_PASSWORD=$env:AETHER_WIFI_PASSWORD",
  '-D','SERVICE_UID={service_uid}',
  '-D','BENCH_CLIENT_ID={BENCH_CLIENT_ID}',
  '-D','AE_EXP_SIMPLE_FULL_HOT_101=1',
  '-D','AE_EXP_THERMOMETER2_FULL_VANILLA=',
  '-D','BOARD=0',
  '-D','AE_EXP_WIFI_CANONICAL=1',
  '-D','CPM_aether-client-cpp_SOURCE={aether}',
  '-D','USER_CONFIG={ucfg}',
  '-D','FS_INIT={fs_init}'
)
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
}}
idf.py @common reconfigure build
"""
    vanilla.ensure_aether_patched()
    vanilla.run_idf(extra)
    bin_path = BUILD / "temperature_sensor.bin"
    if not bin_path.is_file():
        raise RuntimeError("firmware bin missing after build")
    return file_sha256(bin_path)
def parse_partition_size(token: str) -> int:
    s = token.strip().upper()
    if s.endswith("K"):
        return int(s[:-1], 10) * 1024
    if s.endswith("M"):
        return int(s[:-1], 10) * 1024 * 1024
    return int(s, 0)


def storage_partition_bounds() -> tuple[int, int] | None:
    pt_bin = BUILD / "partition_table" / "partition-table.bin"
    if not pt_bin.is_file():
        return None
    idf = os.environ.get("IDF_PATH")
    if not idf:
        guess = Path(r"C:\Espressif\frameworks\esp-idf-v6.0.2")
        if guess.is_dir():
            idf = str(guess)
    gen = (
        Path(idf) / "components" / "partition_table" / "gen_esp32part.py"
        if idf
        else None
    )
    if gen is None or not gen.is_file():
        return None
    try:
        text = subprocess.check_output(
            [sys.executable, str(gen), str(pt_bin)], text=True, stderr=subprocess.STDOUT
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5 and parts[0] == "storage":
            return int(parts[3], 0), parse_partition_size(parts[4])
    return None


def addr2line_panic(elf: Path, mepc: str) -> str | None:
    if not elf.is_file():
        return None
    tool = os.environ.get("IDF_TOOLS_PATH")
    candidates: list[Path] = []
    esp_tools = Path(r"C:\Espressif\tools\riscv32-esp-elf")
    if esp_tools.is_dir():
        for exe in esp_tools.glob("*/riscv32-esp-elf/bin/riscv32-esp-elf-addr2line.exe"):
            candidates.append(exe)
    if tool:
        candidates.append(
            Path(tool) / "tools" / "riscv32-esp-elf" / "riscv32-esp-elf" / "bin" / "riscv32-esp-elf-addr2line.exe"
        )
    candidates.append(Path("riscv32-esp-elf-addr2line"))
    for exe in candidates:
        if not exe.is_file() and exe.name == "riscv32-esp-elf-addr2line":
            continue
        try:
            out = subprocess.check_output(
                [str(exe), "-pfiaC", "-e", str(elf), mepc.replace("0x", "")],
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
            return out or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return None


def esptool_cmd(port: str, *args: str) -> list[str]:
    return [str(sb.IDF_PYTHON), "-m", "esptool", "--chip", "esp32c6", "-p", port, *args]


def capture_storage_diagnostics(port: str | None, run_id: str) -> dict:
    diag_dir = DIAG_LOCAL / run_id
    diag_dir.mkdir(parents=True, exist_ok=True)
    pt_bin = BUILD / "partition_table" / "partition-table.bin"
    if pt_bin.is_file():
        shutil.copy2(pt_bin, diag_dir / "partition-table.bin")
        idf = os.environ.get("IDF_PATH") or r"C:\Espressif\frameworks\esp-idf-v6.0.2"
        gen = Path(idf) / "components" / "partition_table" / "gen_esp32part.py"
        if gen.is_file():
            try:
                pt_text = subprocess.check_output(
                    [sys.executable, str(gen), str(pt_bin)], text=True
                )
                (diag_dir / "partition-table.txt").write_text(pt_text, encoding="utf-8")
            except subprocess.CalledProcessError:
                pass
    bounds = storage_partition_bounds()
    meta: dict = {
        "run_id": run_id,
        "client_map_source": "aether_domain_spiffs_clients_map",
        "fs_init_used": False,
        "bench_client_id": BENCH_CLIENT_ID,
        "missing_object_id": 2079829250,
        "failure_class": "map_entry_present_storage_object_missing",
        "spiffs_physical_corruption": "not_proven",
        "storage_partition": None,
        "spiffs_dump_path": None,
        "panic_addr2line": None,
    }
    if bounds:
        offset, size = bounds
        meta["storage_partition"] = {"offset": offset, "size": size}
        if port:
            dump_path = diag_dir / "storage_spiffs.bin"
            try:
                env = os.environ.copy()
                env.pop("PYTHONPATH", None)
                subprocess.run(
                    esptool_cmd(
                        port,
                        "read_flash",
                        hex(offset),
                        hex(size),
                        str(dump_path),
                    ),
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                meta["spiffs_dump_path"] = str(dump_path)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                meta["spiffs_read_error"] = str(e)
    elf = BUILD / "temperature_sensor.elf"
    meta["panic_addr2line"] = addr2line_panic(elf, "0x4200be46")
    (diag_dir / "storage_diag.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return meta


def erase_aether_storage_partition(port: str) -> None:
    bounds = storage_partition_bounds()
    if not bounds:
        raise RuntimeError("storage partition not found in partition table")
    offset, size = bounds
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.check_call(
        esptool_cmd(port, "erase_region", hex(offset), hex(size)),
        env=env,
    )


def flash_firmware(port: str) -> None:
    b = BUILD.as_posix()
    extra = f"idf.py -B '{b}' -p {port} flash"
    vanilla.run_idf(extra)
def parse_log(log_path: Path, *, test_sender: str | None) -> tuple[list[int], str | None]:
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    nums: list[int] = []
    sender = test_sender
    for m in RE_MSG.finditer(text):
        n = int(m.group(1))
        s = m.group(2).lower()
        if sender is None:
            sender = s
        if s == sender:
            nums.append(n)
    return nums, sender
def evaluate(nums: list[int]) -> dict:
    c = Counter(nums)
    expect = set(range(1, LAST_MESSAGE + 1))
    got = set(c.keys())
    missing = sorted(expect - got)
    dups = sorted(n for n, k in c.items() if k > 1)
    full_rx = 1 if c.get(1, 0) >= 1 else 0
    hot_nums = [n for n in nums if n >= 2]
    hot_unique = len({n for n in hot_nums})
    pass_ok = (
        got == expect
        and len(nums) == LAST_MESSAGE
        and full_rx == 1
        and hot_unique == HOT_COUNT
        and not dups
    )
    return {
        "FULL_RX": f"{full_rx}/1",
        "HOT_RX": f"{hot_unique}/{HOT_COUNT}",
        "missing_numbers": missing,
        "duplicates": dups,
        "msg_rx_count": len(nums),
        "pass": pass_ok,
        "HOT_COUNT": HOT_COUNT,
    }
def infer_actual_attempts(fw: dict) -> str | dict:
    full_tries = len(fw.get("full_write_statuses") or [])
    hot_tries = int(fw.get("hot_begin_count") or 0)
    if not fw.get("hot_rtc_table_complete"):
        if full_tries == 0 and hot_tries == 0:
            return "UNKNOWN"
        return {"full_write_begin": full_tries, "hot_begin": hot_tries}
    rtc_rows = fw.get("hot_rtc_rows") or {}
    started = sum(
        1
        for n, r in rtc_rows.items()
        if r.get("state") not in ("NOT_RUN",)
    )
    hot_tries = max(hot_tries, started)
    if full_tries == 0 and hot_tries == 0:
        return "UNKNOWN"
    return {"full_write_begin": full_tries, "hot_begin": hot_tries}


def analyze_hot_rtc(
    fw: dict, rx_nums: list[int], *, test_sender: str | None
) -> dict | None:
    if not fw.get("hot_rtc_table_complete"):
        return None
    rows = fw.get("hot_rtc_rows") or {}
    rx_hot = {n for n in rx_nums if n >= 2}
    planned = HOT_COUNT
    started = 0
    got_ip_ok = 0
    encode_called = 0
    encode_ok = 0
    nonce_ok = 0
    sendto_called = 0
    sendto_ok = 0
    wifi_fail = 0
    encode_fail = 0
    sendto_fail = 0
    sendto_ok_not_rx: list[int] = []
    not_run: list[int] = []
    unknown: list[int] = []
    for n in range(2, LAST_MESSAGE + 1):
        r = rows.get(n)
        if not r:
            unknown.append(n)
            continue
        st = r.get("state", "")
        if st == "NOT_RUN":
            not_run.append(n)
            continue
        if st in ("UNKNOWN", "IN_PROGRESS"):
            unknown.append(n)
            continue
        started += 1
        if r.get("got_ip"):
            got_ip_ok += 1
        if r.get("encode_called"):
            encode_called += 1
        if r.get("encode_ok") or r.get("encode"):
            encode_ok += 1
        if r.get("nonce"):
            nonce_ok += 1
        if r.get("sendto_called"):
            sendto_called += 1
        if r.get("sendto"):
            sendto_ok += 1
            if n not in rx_hot:
                sendto_ok_not_rx.append(n)
        err = int(r.get("err") or 0)
        if err == 1:
            wifi_fail += 1
        elif err == 2:
            encode_fail += 1
        elif err == 3:
            sendto_fail += 1
    return {
        "HOT_WIFI": fw.get("hot_wifi_mode") or "unknown",
        "HOT_ATTEMPTS": started,
        "GOT_IP_OK": got_ip_ok,
        "ENCODE_CALLED": encode_called,
        "ENCODE_OK": encode_ok,
        "NONCE_OK": nonce_ok,
        "SENDTO_CALLED": sendto_called,
        "SENDTO_OK": sendto_ok,
        "wifi_failures": wifi_fail,
        "encode_failures": encode_fail,
        "sendto_failures": sendto_fail,
        "sendto_ok_but_not_received": sendto_ok_not_rx,
        "NOT_RUN": not_run,
        "UNKNOWN": unknown,
        "hot_rtc_planned": planned,
        "hot_rtc_started": started,
    }
def wait_campaign(
    *,
    rx_log: Path,
    serial_log: Path,
    rx_proc: subprocess.Popen,
    writer: ResultWriter,
    state: dict,
    ppk_capture: SimpleFullHotPpkCapture | None = None,
) -> None:
    clock = SystemMonotonicClock()
    timer_cfg = CampaignTimerConfig(
        usb_startup_timeout_s=USB_STARTUP_TIMEOUT_S,
        bootstrap_extra_timeout_s=BOOTSTRAP_EXTRA_TIMEOUT_S,
        full_active_timeout_s=FULL_ACTIVE_TIMEOUT_S,
        hot_cycle_timeout_s=HOT_CYCLE_TIMEOUT_S,
    )
    timers = CampaignTimerState(
        campaign_start=clock.now(),
        usb_last_progress=clock.now(),
    )
    test_sender: str | None = state.get("test_sender_uid")
    phase = "USB_STARTUP"
    last_rx_num = 0
    last_msg_count = 0
    last_hot_begin = 0
    overall_deadline = clock.now() + smoke_overall_deadline_s(timer_cfg, HOT_COUNT)
    last_marker: str | None = None
    tail_enter: float | None = None
    ppk_serial_seen: set[str] = set()

    def sync_state(extra: dict | None = None) -> None:
        nonlocal test_sender, phase, last_rx_num, last_msg_count, last_hot_begin, last_marker
        now = clock.now()
        rx_text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.is_file() else ""
        ser_text = (
            serial_log.read_text(encoding="utf-8", errors="replace")
            if serial_log.is_file()
            else ""
        )
        if ppk_capture is not None:
            sync_events_from_serial(ppk_capture, ser_text, seen=ppk_serial_seen)
        nums, test_sender = parse_log(rx_log, test_sender=test_sender)
        ev = evaluate(nums)
        fw = parse_firmware_serial(ser_text)
        rx_meta = parse_receiver_meta(rx_text)
        if nums:
            if timers.note_unique_rx(nums, now=now):
                timers.note_usb_progress(now=now)
            last_rx_num = max(nums)
            last_msg_count = len(nums)
        if fw["max_hot_begin"] > last_hot_begin:
            last_hot_begin = fw["max_hot_begin"]
            timers.note_usb_progress(now=now)
        if fw.get("confirmed_failure_stage"):
            timers.note_usb_progress(now=now)
        marker = fw.get("last_firmware_marker")
        if (
            marker
            and "SERIAL_READ_ERROR" not in marker
            and "RX_HEALTH" not in marker
            and not marker.startswith("Index,Invocations")
            and marker != last_marker
        ):
            last_marker = marker
            timers.note_usb_progress(now=now)
        state.update(ev)
        state["phase"] = phase
        state["run_status"] = state.get("run_status", "RUNNING")
        state["test_sender_uid"] = test_sender
        state["receiver_uid_live"] = rx_meta.get("receiver_uid")
        state["receiver_tcp_up"] = rx_meta.get("receiver_tcp_up")
        state["last_rx_num"] = last_rx_num
        state["last_hot_begin"] = last_hot_begin
        state["last_firmware_marker"] = fw.get("last_firmware_marker")
        state["confirmed_failure_stage"] = fw.get("confirmed_failure_stage")
        state["firmware_service_uid"] = fw.get("firmware_service_uid")
        state["expected_sender_uid"] = fw.get("expected_sender_uid")
        state["actual_attempts"] = infer_actual_attempts(fw)
        state["FULL_BEGIN"] = fw.get("full_begin_count", 0)
        state["FULL_WRITE_CALLED"] = fw.get("full_write_called", 0)
        state["HOT_BEGIN"] = fw.get("hot_begin_count", 0)
        state["HOT_SENDTO_OK"] = fw.get("hot_sendto_ok_count", 0)
        state["bootstrap_done"] = fw.get("bootstrap_done")
        state["bootstrap_select_fail"] = fw.get("bootstrap_select_fail")
        state["client_reload_check"] = fw.get("client_reload_check")
        state["full_wifi_psp_present"] = fw.get("full_wifi_psp_present")
        state["wifi_radio_applied"] = fw.get("wifi_radio_applied")
        state["got_ip"] = fw.get("got_ip")
        state["first_wifi_esp_error"] = fw.get("first_wifi_esp_error")
        state["full_write_statuses"] = fw.get("full_write_statuses")
        state["hot_wifi_mode"] = fw.get("hot_wifi_mode")
        state["hot_rtc_table_complete"] = fw.get("hot_rtc_table_complete")
        hot_a = analyze_hot_rtc(fw, nums, test_sender=test_sender)
        if hot_a is not None:
            state["hot_rtc_analysis"] = hot_a
        if extra:
            state.update(extra)
        writer.write(state)
        print(
            f"PHASE={phase} last_rx={last_rx_num} hot_begin={last_hot_begin} "
            f"marker={state.get('last_firmware_marker')}",
            flush=True,
        )
    sync_state()
    receiver_exited = False
    while clock.now() < overall_deadline:
        now = clock.now()
        prev_phase = phase
        ser_text = (
            serial_log.read_text(encoding="utf-8", errors="replace")
            if serial_log.is_file()
            else ""
        )
        fw = parse_firmware_serial(ser_text)
        if rx_proc.poll() is not None and not receiver_exited:
            receiver_exited = True
            if last_rx_num >= LAST_MESSAGE or fw.get("scenario_done"):
                phase = "TAIL"
                state["error"] = (
                    state.get("error")
                    or f"receiver_exit_code={rx_proc.returncode}_after_delivery"
                )
                timers.note_usb_progress(now=now)
            else:
                phase = "RECEIVER_EXIT"
                state["run_status"] = "FAIL"
                state["error"] = f"receiver_exit_code={rx_proc.returncode}"
                sync_state()
                return
        full_begin_seen = bool(
            RE_FULL_BEGIN.search(ser_text) or "FULL_WRITE_BEGIN" in ser_text
        )
        if full_begin_seen and timers.full_begin is None:
            timers.mark_full_begin(now=now)
            phase = "FULL"
        if "Guru Meditation Error" in ser_text or "panic'ed" in ser_text:
            phase = "ESP_PANIC"
            state["run_status"] = "FAIL"
            state["error"] = "esp_panic_in_serial"
            if "Select loaded client" in ser_text:
                state["confirmed_failure_stage"] = "select_client_panic_load_fault"
            else:
                state["confirmed_failure_stage"] = "esp_panic"
            sync_state()
            time.sleep(TAIL_DELIVERY_S)
            sync_state()
            return
        if fw.get("bootstrap_select_fail") and not fw.get("bootstrap_done"):
            phase = "BOOTSTRAP_FAIL"
            state["run_status"] = "FAIL"
            state["confirmed_failure_stage"] = "bootstrap_select"
            state["error"] = fw.get("fail_error") or "bootstrap_select_failed"
            state["HOT"] = "NOT_RUN"
            sync_state()
            time.sleep(TAIL_DELIVERY_S)
            sync_state()
            return
        if fw.get("confirmed_failure_stage"):
            phase = "SCENARIO_FAIL"
            state["run_status"] = "FAIL"
            state["error"] = fw.get("fail_error") or fw["confirmed_failure_stage"]
            if not fw.get("hot_begin_count"):
                state["HOT"] = "NOT_RUN"
            sync_state()
            time.sleep(TAIL_DELIVERY_S)
            sync_state()
            return
        if RE_SELECT_FAIL.search(ser_text) and "error=3" in ser_text:
            phase = "SELECT_LOAD_FAIL"
            state["run_status"] = "FAIL"
            state["confirmed_failure_stage"] = "select_client_load_failed"
            state["error"] = "select_client_error_3_no_panic"
            sync_state()
            time.sleep(TAIL_DELIVERY_S)
            sync_state()
            return
        nums_tail, _ = parse_log(rx_log, test_sender=test_sender)
        ev_tail = evaluate(nums_tail)
        if last_rx_num >= LAST_MESSAGE and ev_tail.get("FULL_RX") == "1/1":
            phase = "TAIL"
            if tail_enter is None:
                tail_enter = now
            timers.note_usb_progress(now=now)
        if (
            phase == "TAIL"
            and tail_enter is not None
            and now - tail_enter >= TAIL_DELIVERY_S
        ):
            sync_state()
            return
        if fw.get("scenario_done") or fw.get("confirmed_failure_stage"):
            phase = "TAIL"
            timers.note_usb_progress(now=now)
        if fw.get("hot_rtc_table_complete") and (
            fw.get("scenario_done")
            or fw.get("confirmed_failure_stage")
            or last_rx_num >= LAST_MESSAGE
        ):
            phase = "FIRMWARE_DONE"
            sync_state()
            time.sleep(TAIL_DELIVERY_S)
            sync_state()
            return
        hot_lane_active = (
            fw["max_hot_begin"] >= 2
            or int(fw.get("hot_begin_count") or 0) >= 1
            or last_rx_num >= 2
            or "PREPARED_EXPORT_RESULT=ok" in ser_text
        )
        if timers.full_begin is not None:
            if hot_lane_active:
                phase = "HOT"
            else:
                phase = "FULL"
        elif (
            fw.get("bootstrap_done")
            or "PREPARED_EXPORT_RESULT=ok" in ser_text
            or last_rx_num >= 1
        ):
            if hot_lane_active:
                phase = "HOT"
            else:
                phase = "POST_BOOTSTRAP"
        if phase == "HOT" and prev_phase != "HOT":
            timers.mark_hot_begin(now=now)
        if phase == "HOT" and last_rx_num >= LAST_MESSAGE:
            phase = "TAIL"
        dl = evaluate_deadlines(
            phase=phase,
            cfg=timer_cfg,
            timers=timers,
            now=now,
            ser_text=ser_text,
            fw=fw,
            last_rx_num=last_rx_num,
            last_message=LAST_MESSAGE,
            hot_lane_active=hot_lane_active,
            full_begin_seen=full_begin_seen,
        )
        deadline_hit = dl.hit
        if dl.hit:
            state["error"] = dl.error
            phase = dl.phase_tag or phase
        sync_state()
        if deadline_hit:
            state["run_status"] = "TIMEOUT"
            sync_state()
            time.sleep(TAIL_DELIVERY_S)
            sync_state()
            return
        time.sleep(1.0)
    state["run_status"] = "TIMEOUT"
    state["error"] = state.get("error") or "overall_deadline"
    phase = "OVERALL_TIMEOUT"
    sync_state()
def classify_delivery(state: dict) -> str:
    full_ok = state.get("FULL_RX") == "1/1"
    hot_rx = state.get("HOT_RX", f"0/{HOT_COUNT}")
    try:
        got, need = hot_rx.split("/")
        hot_ok = int(got) >= int(need) and int(need) == HOT_COUNT
    except ValueError:
        hot_ok = False
    if full_ok and hot_ok:
        return "PASS"
    if full_ok or state.get("msg_rx_count", 0) > 0:
        return "PARTIAL"
    return "FAIL"


def classify_rtc_capture(state: dict) -> str:
    if state.get("hot_rtc_table_complete"):
        return "COMPLETE"
    blocks = int(state.get("hot_rtc_dump_blocks") or 0)
    if blocks > 0 or state.get("hot_rtc_rows"):
        return "PARTIAL"
    return "MISSING"


def repeated_rtc_dump_verified(text: str) -> bool:
    blocks = extract_rtc_dumps(text, hot_count=HOT_COUNT)
    good = [
        b
        for b in blocks
        if b.complete and pick_authoritative_rtc_dump([b], hot_count=HOT_COUNT)
    ]
    if len(good) < 2:
        return False
    a, b = good[-2], good[-1]
    if a.run_id is not None and b.run_id is not None and a.run_id != b.run_id:
        return False
    return a.rows == b.rows


def execution_status(state: dict) -> str:
    rs = state.get("run_status")
    if state.get("runner_interrupted"):
        return "ABORTED"
    if rs in ("PASS", "DONE", "FAIL"):
        return "COMPLETED"
    if rs == "TIMEOUT":
        return "TIMEOUT"
    if rs == "ABORTED":
        return "ABORTED"
    if rs == "RUNNING":
        return "ABORTED"
    return "COMPLETED"


def finalize_stale_running_results() -> list[str]:
    """Mark prior RUNNING result JSON as ABORTED (shell killed runner before finally)."""
    fixed: list[str] = []
    for path in sorted(RESULTS.glob("result_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("run_status") != "RUNNING":
            continue
        data["run_status"] = "ABORTED"
        data["runner_interrupted"] = True
        data["error"] = data.get("error") or "stale_run_finalized_on_next_runner_start"
        if data.get("TEST") == "NOT_RUN":
            data["TEST"] = "ABORTED"
        finalize_test_status(data)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        fixed.append(path.name)
    return fixed


def finalize_test_status(state: dict) -> None:
    state["DELIVERY"] = classify_delivery(state)
    state["RTC_CAPTURE"] = classify_rtc_capture(state)
    state["POWER_CAPTURE"] = classify_power_capture_status(state.get("power_capture"))
    state["campaign_timer_monotonic"] = True
    state["last_rx_progress_unique_only"] = True
    if state.get("run_status") in ("RUNNING", None):
        rs = state.get("run_status") or "FAIL"
        if state["DELIVERY"] == "PASS" and state["RTC_CAPTURE"] == "COMPLETE":
            state["run_status"] = "PASS"
        elif rs == "TIMEOUT":
            state["run_status"] = "TIMEOUT"
        else:
            state["run_status"] = "FAIL" if state["DELIVERY"] == "FAIL" else "DONE"
    state["EXECUTION"] = execution_status(state)
    if state.get("TEST") in ("PARTIAL", "PASS", "FAIL", "TIMEOUT", "ABORTED"):
        return
    if (
        state["DELIVERY"] == "PASS"
        and state["RTC_CAPTURE"] == "COMPLETE"
        and state["POWER_CAPTURE"] == "COMPLETE"
    ):
        state["TEST"] = "PASS"
    elif state["DELIVERY"] == "PARTIAL":
        state["TEST"] = "PARTIAL"
    elif state.get("run_status") in ("TIMEOUT", "ABORTED"):
        state["TEST"] = state["run_status"]
    else:
        state["TEST"] = "FAIL"
    hot_begin = int(state.get("HOT_BEGIN") or 0)
    ha = state.get("hot_rtc_analysis") or {}
    if ha.get("HOT_ATTEMPTS"):
        hot_begin = max(hot_begin, int(ha["HOT_ATTEMPTS"]))
    if state.get("HOT") is None:
        state["HOT"] = (
            f"RUN/{hot_begin}" if hot_begin else "NOT_RUN"
        )


def write_hot_rtc_csv(path: Path, fw: dict) -> None:
    rows = fw.get("hot_rtc_rows") or {}
    lines = [
        "n,got_ip,encode_called,encode_ok,nonce,sendto_called,sendto,err,state"
    ]
    for n in range(2, LAST_MESSAGE + 1):
        r = rows.get(n, {})
        lines.append(
            f"{n},{r.get('got_ip','')},{r.get('encode_called','')},"
            f"{r.get('encode_ok', r.get('encode',''))},{r.get('nonce','')},"
            f"{r.get('sendto_called','')},{r.get('sendto','')},"
            f"{r.get('err','')},{r.get('state','')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
def main() -> int:
    stale = finalize_stale_running_results()
    if stale:
        print(f"FINALIZED_STALE_RUNS={','.join(stale)}", flush=True)
    run_id = utc_tag()
    log_path = RESULTS / f"receiver_{run_id}.log"
    serial_path = RESULTS / f"serial_{run_id}.log"
    result_path = RESULTS / f"result_{run_id}.json"
    latest_path = RESULTS / "LATEST.json"
    session = HERE / f"simple_full_hot_101_rx_session_{run_id}"
    writer = ResultWriter(result_path, latest_path)
    aether_meta = vanilla.ensure_aether_patched()
    head = git_head()
    local_changes = git_status_porcelain().strip()
    state: dict = {
        "run_id": run_id,
        "phase": "INIT",
        "run_status": "RUNNING",
        "git_head": head,
        "git_local_changes": local_changes or "(clean)",
        "aether_meta": aether_meta,
        "SERVICE_UID": None,
        "receiver_sha256": None,
        "firmware_sha256": None,
        "FLASH": "NOT_RUN",
        "TEST": "NOT_RUN",
        "USB_ACCESS": None,
        "logs": {
            "receiver": str(log_path.relative_to(ROOT)),
            "serial": str(serial_path.relative_to(ROOT)),
        },
        "result_json": str(result_path.relative_to(ROOT)),
        "actual_attempts": "UNKNOWN",
    }
    writer.write(state)
    interrupted = {"flag": False}
    def on_signal(_sig, _frame) -> None:
        interrupted["flag"] = True
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)
    ppk: sb.PpkSession | None = None
    ppk_capture: SimpleFullHotPpkCapture | None = None
    ppk_raw: Path | None = None
    rx_proc: subprocess.Popen | None = None
    serial_cap: SerialCapture | None = None
    try:
        ssid, pwd = vanilla.load_wifi()
        rx_exe, rx_sha = build_receiver()
        state["receiver_sha256"] = rx_sha
        writer.write(state)
        rx_proc = start_receiver(rx_exe, log_path, session)
        uid = wait_uid(log_path)
        state["SERVICE_UID"] = uid
        writer.write(state)
        print(f"RECEIVER_UID={uid}", flush=True)
        stop_experiment_holders()
        vanilla.stop_stage1_serial_holders()
        ppk = vanilla.open_ppk_session()
        ppk.set_on()
        bin_path = BUILD / "temperature_sensor.bin"
        skip_build = os.environ.get("SIMPLE_FULL_HOT_SKIP_BUILD", "").strip() == "1"
        state["phase"] = "BUILD"
        state["hot_count"] = HOT_COUNT
        state["smoke_overall_deadline_s"] = smoke_overall_deadline_s(
            CampaignTimerConfig(
                usb_startup_timeout_s=USB_STARTUP_TIMEOUT_S,
                bootstrap_extra_timeout_s=BOOTSTRAP_EXTRA_TIMEOUT_S,
                full_active_timeout_s=FULL_ACTIVE_TIMEOUT_S,
                hot_cycle_timeout_s=HOT_CYCLE_TIMEOUT_S,
            ),
            HOT_COUNT,
        )
        writer.write(state)
        if skip_build and bin_path.is_file():
            fw_sha = file_sha256(bin_path)
            state["build"] = "skipped_existing_bin"
        else:
            fw_sha = build_esp(service_uid=uid, ssid=ssid, pwd=pwd)
            state["build"] = "fresh"
        state["firmware_sha256"] = fw_sha
        state["fs_init_header"] = str(FS_INIT_HEADER.relative_to(ROOT))
        state["fs_init_sha256"] = file_sha256(FS_INIT_HEADER)
        state["user_config"] = str(USER_CONFIG.relative_to(ROOT))
        writer.write(state)
        port = recover_esp_port(ppk)
        storage_diag = capture_storage_diagnostics(port, run_id)
        state["storage_diag_summary"] = {
            k: storage_diag.get(k)
            for k in (
                "failure_class",
                "missing_object_id",
                "fs_init_used",
                "spiffs_physical_corruption",
                "panic_addr2line",
            )
        }
        writer.write(state)
        state["storage_erase"] = "skipped"
        writer.write(state)
        if os.environ.get("SIMPLE_ERASE_AETHER_STORAGE", "").strip() == "1":
            if not port:
                raise RuntimeError("erase requested but no COM port")
            print("Erasing Aether SPIFFS storage partition only", flush=True)
            erase_aether_storage_partition(port)
            state["storage_erase"] = "storage_partition_only"
            writer.write(state)
        if not port:
            state["USB_ACCESS"] = "BLOCKED"
            state["FLASH"] = "BLOCKED"
            state["run_status"] = "ABORTED"
            state["TEST"] = "NOT_RUN"
            state["FULL_RX"] = "0/1"
            state["HOT_RX"] = f"0/{HOT_COUNT}"
            state["missing_numbers"] = list(range(1, LAST_MESSAGE + 1))
            state["duplicates"] = []
            finalize_test_status(state)
            writer.write(state)
            print(f"TEST={state['TEST']}", flush=True)
            return 2
        state["USB_ACCESS"] = "OK"
        state["phase"] = "FLASH"
        writer.write(state)
        flash_firmware(port)
        state["FLASH"] = "PASS"
        state["phase"] = "POST_FLASH"
        writer.write(state)
        print(f"FLASH=PASS port={port}", flush=True)
        ppk_raw = RESULTS / f"ppk_raw_{run_id}.bin"
        ppk_capture = SimpleFullHotPpkCapture(
            ppk, ppk_raw, voltage_mv=VOLTAGE_MV, voltage_source="set_source_meter"
        )
        ppk_capture.start()
        ppk.set_on()
        ppk_capture.mark_cold_boot()
        vanilla.cold_boot_dut(ppk, off_s=4.0, wait_com_s=20.0)
        time.sleep(2.0)
        post_port = sb.wait_espressif_com(timeout_s=45.0) or port
        serial_cap = SerialCapture(serial_path)
        serial_cap.start(post_port)
        state["phase"] = "CAMPAIGN"
        writer.write(state)
        wait_campaign(
            rx_log=log_path,
            serial_log=serial_path,
            rx_proc=rx_proc,
            writer=writer,
            state=state,
            ppk_capture=ppk_capture,
        )
        if interrupted["flag"]:
            state["run_status"] = "ABORTED"
            state["runner_interrupted"] = True
            state["error"] = "runner_interrupted"
        rtc_deadline = time.time() + 240.0
        while serial_cap is not None and time.time() < rtc_deadline:
            ser_text = (
                serial_path.read_text(encoding="utf-8", errors="replace")
                if serial_path.is_file()
                else ""
            )
            if ppk_capture is not None:
                seen_rtc = {e.name for e in ppk_capture.events}
                sync_events_from_serial(ppk_capture, ser_text, seen=seen_rtc)
            if repeated_rtc_dump_verified(ser_text):
                break
            time.sleep(2.0)
        if ppk_capture is not None:
            ppk_meta = ppk_capture.stop()
            state["power_capture"] = analyze_power_capture(
                meta=ppk_meta,
                events=ppk_capture.events,
                raw_bin=ppk_raw,
                voltage_mv=VOLTAGE_MV,
                hot_count=HOT_COUNT,
            )
        if serial_cap is not None:
            serial_cap.stop()
            serial_cap = None
        ser_text = (
            serial_path.read_text(encoding="utf-8", errors="replace")
            if serial_path.is_file()
            else ""
        )
        nums, test_sender = parse_log(log_path, test_sender=state.get("test_sender_uid"))
        state.update(evaluate(nums))
        state["test_sender_uid"] = test_sender
        fw_final = parse_firmware_serial(ser_text)
        state["hot_rtc_dump_blocks"] = fw_final.get("hot_rtc_dump_blocks", 0)
        hot_a = analyze_hot_rtc(fw_final, nums, test_sender=test_sender)
        if hot_a is not None:
            state["hot_rtc_analysis"] = hot_a
            state.update(
                {
                    k: hot_a[k]
                    for k in hot_a
                    if k.startswith(
                        (
                            "HOT_",
                            "GOT_",
                            "ENCODE_",
                            "SENDTO_",
                            "NONCE_",
                            "wifi_",
                            "encode_",
                            "sendto_",
                            "NOT_RUN",
                            "UNKNOWN",
                            "hot_rtc",
                        )
                    )
                }
            )
            state["local_failures"] = {
                "wifi": hot_a.get("wifi_failures", 0),
                "encode": hot_a.get("encode_failures", 0),
                "sendto": hot_a.get("sendto_failures", 0),
            }
        # One capture thread does not verify COM ownership across processes.
        state["serial_single_owner_verified"] = False
        state["repeated_dump_verified"] = repeated_rtc_dump_verified(ser_text)
        csv_path = RESULTS / f"hot_rtc_{run_id}.csv"
        write_hot_rtc_csv(csv_path, fw_final)
        state["artifacts"] = {
            "result_json": str(result_path.relative_to(ROOT)),
            "hot_rtc_csv": str(csv_path.relative_to(ROOT)),
            "receiver_log": str(log_path.relative_to(ROOT)),
            "serial_log": str(serial_path.relative_to(ROOT)),
        }
        if ppk_capture is not None:
            state["artifacts"]["ppk_raw_bin"] = str(ppk_raw.relative_to(ROOT))
            state["artifacts"]["ppk_meta_json"] = str(
                ppk_raw.with_suffix(".json").relative_to(ROOT)
            )
            state["artifacts"]["ppk_events_json"] = str(
                ppk_raw.with_suffix(".events.json").relative_to(ROOT)
            )
        finalize_test_status(state)
        if state["TEST"] == "PASS":
            state["run_status"] = "PASS"
        writer.write(state)
        print(f"TEST={state['TEST']}", flush=True)
        print(json.dumps(state, indent=2), flush=True)
        return 0 if state["TEST"] == "PASS" else 1
    except Exception as e:
        state["run_status"] = "FAIL"
        state["error"] = str(e)
        finalize_test_status(state)
        writer.write(state)
        print(f"TEST={state['TEST']}", flush=True)
        return 1
    finally:
        if state.get("run_status") == "RUNNING":
            state["run_status"] = "ABORTED"
            state["runner_interrupted"] = True
            state["error"] = state.get("error") or "runner_exited_while_running"
            finalize_test_status(state)
            try:
                writer.write(state)
            except Exception:
                pass
        if serial_cap is not None:
            serial_cap.stop()
        if rx_proc is not None and rx_proc.poll() is None:
            rx_proc.terminate()
            try:
                rx_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rx_proc.kill()
        if ppk_capture is not None and ppk_capture.active:
            try:
                ppk_capture.stop()
            except Exception:
                pass
        if ppk is not None:
            try:
                ppk.set_off()
            except Exception:
                pass
if __name__ == "__main__":
    raise SystemExit(main())
