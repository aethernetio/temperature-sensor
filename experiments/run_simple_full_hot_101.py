#!/usr/bin/env python3
"""One run: 1× FULL vanilla + 100× HOT (2 s sleep). No PPK capture."""
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
USER_CONFIG = ROOT / "main" / "user_config_full_vanilla.h"
RX_SRC = PREPARED / "temperature_receiver"
RX_BUILD = HERE / "simple_full_hot_101_rx_build"
BENCH_CLIENT_ID = "reliability_full_v1"
VOLTAGE_MV = 3000
USB_STARTUP_TIMEOUT_S = 90.0
BOOTSTRAP_EXTRA_TIMEOUT_S = 180.0
DIAG_LOCAL = RESULTS / "diag_local"
FULL_ACTIVE_TIMEOUT_S = 120.0
HOT_CYCLE_TIMEOUT_S = 52.0
TAIL_DELIVERY_S = 15.0
HOT_COUNT = 100
SLEEP_S = 2
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
class SerialCapture:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._port: str | None = None
    def start(self, port: str) -> None:
        self._port = port
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
    def _run(self) -> None:
        import serial

        port = self._port
        assert port
        deadline = time.time() + 45.0
        ser = None
        while time.time() < deadline and not self._stop.is_set():
            try:
                ser = serial.Serial(port, 115200, timeout=0.4)
                break
            except Exception as e:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(f"SERIAL_OPEN_RETRY={e}\n")
                time.sleep(1.0)
        if ser is None:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write("SERIAL_CAPTURE_ERROR=open_timeout\n")
            return
        try:
            with ser:
                with self.log_path.open("a", encoding="utf-8", errors="replace") as f:
                    while not self._stop.is_set():
                        try:
                            data = ser.read(4096)
                        except Exception as e:
                            f.write(f"SERIAL_READ_ERROR={e}\n")
                            f.flush()
                            time.sleep(0.5)
                            continue
                        if data:
                            f.write(data.decode("utf-8", errors="replace"))
                            f.flush()
        except Exception as e:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"\nSERIAL_CAPTURE_ERROR={e}\n")


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
    return out
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
        f"{ROOT.as_posix()}/sdkconfig.defaults.thermometer2_aether_full_vanilla"
    )
    sdkconfig = f"{b}/sdkconfig"
    ssid_ps = ssid.replace("'", "''")
    pwd_ps = pwd.replace("'", "''")
    aether = AETHER.as_posix()
    ucfg = USER_CONFIG.as_posix()
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
  '-D','USER_CONFIG={ucfg}'
)
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py @common set-target esp32c6
}}
idf.py @common build
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
    expect = set(range(1, 102))
    got = set(c.keys())
    missing = sorted(expect - got)
    dups = sorted(n for n, k in c.items() if k > 1)
    full_rx = 1 if c.get(1, 0) >= 1 else 0
    hot_nums = [n for n in nums if n >= 2]
    hot_unique = len({n for n in hot_nums})
    pass_ok = (
        got == expect
        and len(nums) == 101
        and full_rx == 1
        and hot_unique == 100
        and not dups
    )
    return {
        "FULL_RX": f"{full_rx}/1",
        "HOT_RX": f"{hot_unique}/100",
        "missing_numbers": missing,
        "duplicates": dups,
        "msg_rx_count": len(nums),
        "pass": pass_ok,
    }
def infer_actual_attempts(fw: dict) -> str | dict:
    full_tries = len(fw.get("full_write_statuses") or [])
    hot_tries = int(fw.get("hot_begin_count") or 0)
    if full_tries == 0 and hot_tries == 0 and not fw.get("full_write_statuses"):
        if fw.get("firmware_service_uid") or fw.get("last_firmware_marker"):
            return {"full_begin": 1 if fw.get("firmware_service_uid") else 0, "hot_begin": hot_tries}
        return "UNKNOWN"
    return {"full_write_begin": full_tries, "hot_begin": hot_tries}
def wait_campaign(
    *,
    rx_log: Path,
    serial_log: Path,
    rx_proc: subprocess.Popen,
    writer: ResultWriter,
    state: dict,
) -> None:
    test_sender: str | None = state.get("test_sender_uid")
    phase = "USB_STARTUP"
    last_progress = time.time()
    full_begin_at: float | None = None
    last_rx_num = 0
    last_hot_begin = 0
    overall_deadline = time.time() + (
        USB_STARTUP_TIMEOUT_S
        + FULL_ACTIVE_TIMEOUT_S
        + HOT_COUNT * HOT_CYCLE_TIMEOUT_S
        + TAIL_DELIVERY_S
        + 30.0
    )
    last_marker: str | None = None

    def sync_state(extra: dict | None = None) -> None:
        nonlocal test_sender, phase, last_rx_num, last_hot_begin, last_progress, last_marker
        rx_text = rx_log.read_text(encoding="utf-8", errors="replace") if rx_log.is_file() else ""
        ser_text = (
            serial_log.read_text(encoding="utf-8", errors="replace")
            if serial_log.is_file()
            else ""
        )
        nums, test_sender = parse_log(rx_log, test_sender=test_sender)
        ev = evaluate(nums)
        fw = parse_firmware_serial(ser_text)
        rx_meta = parse_receiver_meta(rx_text)
        if nums:
            last_rx_num = max(nums)
            last_progress = time.time()
        if fw["max_hot_begin"] > last_hot_begin:
            last_hot_begin = fw["max_hot_begin"]
            last_progress = time.time()
        if fw.get("confirmed_failure_stage"):
            last_progress = time.time()
        marker = fw.get("last_firmware_marker")
        if marker and marker != last_marker:
            last_marker = marker
            last_progress = time.time()
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
        if extra:
            state.update(extra)
        writer.write(state)
        print(
            f"PHASE={phase} last_rx={last_rx_num} hot_begin={last_hot_begin} "
            f"marker={state.get('last_firmware_marker')}",
            flush=True,
        )
    sync_state()
    while time.time() < overall_deadline:
        if rx_proc.poll() is not None:
            phase = "RECEIVER_EXIT"
            state["run_status"] = "FAIL"
            state["error"] = f"receiver_exit_code={rx_proc.returncode}"
            sync_state()
            return
        ser_text = (
            serial_log.read_text(encoding="utf-8", errors="replace")
            if serial_log.is_file()
            else ""
        )
        fw = parse_firmware_serial(ser_text)
        if RE_FULL_BEGIN.search(ser_text) and full_begin_at is None:
            full_begin_at = time.time()
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
        if fw.get("scenario_done"):
            phase = "TAIL"
            last_progress = time.time()
        nums, test_sender = parse_log(rx_log, test_sender=test_sender)
        ev = evaluate(nums)
        if ev["pass"]:
            phase = "PASS"
            state["run_status"] = "PASS"
            sync_state()
            time.sleep(TAIL_DELIVERY_S)
            sync_state()
            return
        now = time.time()
        if phase == "USB_STARTUP" and RE_BOOT.search(ser_text):
            phase = "FULL" if full_begin_at else "USB_STARTUP"
            last_progress = now
        if full_begin_at is not None:
            if (
                fw["max_hot_begin"] >= 2
                or last_rx_num >= 1
                or "PREPARED_EXPORT_RESULT=ok" in ser_text
            ):
                phase = "HOT"
            else:
                phase = "FULL"
        deadline_hit = False
        if full_begin_at is None:
            usb_limit = USB_STARTUP_TIMEOUT_S
            if "BOOTSTRAP_BEGIN" in ser_text and not fw.get("bootstrap_done"):
                usb_limit += BOOTSTRAP_EXTRA_TIMEOUT_S
            if now - last_progress > usb_limit:
                deadline_hit = True
                state["error"] = "usb_startup_timeout_no_full_begin"
                phase = "USB_TIMEOUT"
        elif phase == "FULL":
            if now - full_begin_at > FULL_ACTIVE_TIMEOUT_S:
                deadline_hit = True
                state["error"] = "full_phase_timeout"
                phase = "FULL_TIMEOUT"
        elif phase == "HOT":
            if now - last_progress > HOT_CYCLE_TIMEOUT_S:
                deadline_hit = True
                state["error"] = "hot_cycle_timeout"
                phase = "HOT_TIMEOUT"
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
def finalize_test_status(state: dict) -> None:
    rs = state.get("run_status", "FAIL")
    if rs == "PASS":
        state["TEST"] = "PASS"
    elif rs in ("TIMEOUT", "ABORTED"):
        state["TEST"] = rs
    else:
        state["TEST"] = "FAIL"
    if state.get("HOT") is None:
        hot_begin = int(state.get("HOT_BEGIN") or 0)
        state["HOT"] = "NOT_RUN" if hot_begin == 0 and state["TEST"] != "PASS" else (
            f"RUN/{hot_begin}" if hot_begin else "NOT_RUN"
        )
def main() -> int:
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
        state["phase"] = "BUILD"
        writer.write(state)
        fw_sha = build_esp(service_uid=uid, ssid=ssid, pwd=pwd)
        state["firmware_sha256"] = fw_sha
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
            state["HOT_RX"] = "0/100"
            state["missing_numbers"] = list(range(1, 102))
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
        ppk.set_on()
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
        )
        if interrupted["flag"]:
            state["run_status"] = "ABORTED"
            state["error"] = "runner_interrupted"
        nums, test_sender = parse_log(log_path, test_sender=state.get("test_sender_uid"))
        state.update(evaluate(nums))
        state["test_sender_uid"] = test_sender
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
        if serial_cap is not None:
            serial_cap.stop()
        if rx_proc is not None and rx_proc.poll() is None:
            rx_proc.terminate()
            try:
                rx_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rx_proc.kill()
        if ppk is not None:
            try:
                ppk.set_off()
            except Exception:
                pass
if __name__ == "__main__":
    raise SystemExit(main())
