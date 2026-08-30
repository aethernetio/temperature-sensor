"""VAL100 combined winners — one flash, no COM after FLASH_OK."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
BUILD = ROOT / "build-esp32c6-save-bench-smoke"
AETHER = r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0"
PY = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
CMAKE = Path(r"C:\Espressif\tools\cmake\3.30.2\bin\cmake.exe")
NINJA = Path(r"C:\Espressif\tools\ninja\1.12.1\ninja.exe")
RX_EXE = ROOT / "temperature_receiver" / "build-bisect" / "temperature_receiver.exe"
RX_BUILD = ROOT / "temperature_receiver" / "build-bisect"
RX_SESSION = ROOT / "experiments" / "prepared_wifi_cache_rx_session"
IDF_PATH = r"C:\Espressif\frameworks\esp-idf-v6.0.2"
CCACHE = r"C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64"
PROGRESS = ROOT / "experiments" / "boot_wifi_val100_progress.log"
RX_LOG = ROOT / "experiments" / "prepared_boot_wifi_val100_rx.log"
TSV = ROOT / "experiments" / "prepared_boot_wifi_val100.tsv"
PORT = "COM7"
HOT_TARGET = 100


def env() -> dict:
    e = os.environ.copy()
    e["IDF_PATH"] = IDF_PATH
    e["IDF_TOOLS_PATH"] = r"C:\Espressif"
    extra = [
        CCACHE,
        r"C:\Espressif\tools\ninja\1.12.1",
        r"C:\Espressif\tools\cmake\3.30.2\bin",
        r"C:\msys64\ucrt64\bin",
    ]
    e["Path"] = ";".join(extra) + ";" + e.get("Path", "")
    e.pop("CCACHE_DISABLE", None)
    return e


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def force_sdk_fixes() -> None:
    sdk = BUILD / "sdkconfig"
    if not sdk.exists():
        return
    text = sdk.read_text(encoding="utf-8")
    reps = [
        ("CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y", "# CONFIG_ESP_WIFI_ENABLE_WPA3_SAE is not set"),
        ("CONFIG_RTC_CLK_SRC_INT_RC=y", "# CONFIG_RTC_CLK_SRC_INT_RC is not set"),
        ("# CONFIG_RTC_CLK_SRC_EXT_CRYS is not set", "CONFIG_RTC_CLK_SRC_EXT_CRYS=y"),
        ("# CONFIG_ESP_BROWNOUT_DET is not set", "CONFIG_ESP_BROWNOUT_DET=y"),
        ("CONFIG_PM_ENABLE=y", "# CONFIG_PM_ENABLE is not set"),
    ]
    for a, b in reps:
        text = text.replace(a, b)
    if "CONFIG_RTC_CLK_SRC_EXT_CRYS=y" not in text:
        text += "\nCONFIG_RTC_CLK_SRC_EXT_CRYS=y\n"
    sdk.write_text(text, encoding="utf-8")


def kill_receiver() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "temperature_receiver.exe"],
        capture_output=True,
        text=True,
    )
    time.sleep(1)


def rebuild_receiver() -> None:
    log("rebuild temperature_receiver")
    r = subprocess.run(
        [str(CMAKE), "--build", str(RX_BUILD), "--parallel"],
        env=env(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("receiver build failed")
    log("receiver build ok")


def start_receiver() -> None:
    kill_receiver()
    RX_SESSION.mkdir(parents=True, exist_ok=True)
    if TSV.exists():
        TSV.unlink()
    env2 = env()
    env2["AE_RECEIVER_SESSION_DIR"] = str(RX_SESSION)
    env2["AE_DS_TSV"] = str(TSV)
    with RX_LOG.open("w", encoding="utf-8") as outf, (
        ROOT / "experiments" / "prepared_boot_wifi_val100_rx.log.err"
    ).open("w", encoding="utf-8") as errf:
        subprocess.Popen(
            [str(RX_EXE)],
            cwd=str(RX_SESSION),
            env=env2,
            stdout=outf,
            stderr=errf,
        )
    t0 = time.time()
    while time.time() - t0 < 60:
        text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.exists() else ""
        if "RECEIVER_UID=" in text:
            log("receiver ready")
            return
        time.sleep(1)
    raise RuntimeError("receiver not ready")


def cmake_configure() -> None:
    args = [
        str(CMAKE),
        "-S",
        str(ROOT),
        "-B",
        str(BUILD),
        "-G",
        "Ninja",
        f"-DCPM_aether-client-cpp_SOURCE={AETHER}",
        "-DAE_EXP_PREPARED_BOOT_WIFI_VAL100=1",
        "-DAE_EXP_PREPARED_BOOT_WIFI_OPT=",
        "-DAE_EXP_PREPARED_MAC_RETRY_DIAG=",
        "-DAE_EXP_PREPARED_TX_DONE_DIAG=",
        "-DAE_EXP_PREPARED_DEEPSLEEP_5X50=",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DSERVICE_UID=5aade50f-00d9-4624-b097-e203cdcf1e38",
        "-DBENCH_CLIENT_ID=prepared_deepsleep_5x50_v1",
        "-DAETHER_PREPARED_NONCE_RESERVE=110",
        "-DWIFI_SSID=chirkov",
        "-DWIFI_PASSWORD=kcdjepWz51",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    log("cmake configure VAL100")
    r = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "boot_wifi_val100_cmake.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("cmake failed")
    force_sdk_fixes()
    log("cmake ok")


def ninja_build() -> None:
    log("ninja build")
    r = subprocess.run(
        [str(NINJA), "-C", str(BUILD)], env=env(), capture_output=True, text=True
    )
    if r.returncode != 0:
        (ROOT / "experiments" / "boot_wifi_val100_build.err").write_text(
            (r.stdout or "")[-16000:] + "\n" + (r.stderr or "")[-8000:],
            encoding="utf-8",
        )
        raise RuntimeError("ninja failed")
    log("build ok")


def wait_com_for_flash_only(timeout_s: float = 180.0) -> None:
    log(f"pre-flash: waiting up to {int(timeout_s)}s for {PORT}")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-PnpDevice -Class Ports -Status OK | Where-Object {{ $_.FriendlyName -match '{PORT}' }} | Select-Object -ExpandProperty FriendlyName",
            ],
            capture_output=True,
            text=True,
        )
        if PORT in (r.stdout or ""):
            log(f"pre-flash: {PORT} present")
            return
        time.sleep(2.0)
    raise RuntimeError(f"{PORT} not available — reset ESP once")


def flash_once() -> None:
    wait_com_for_flash_only()
    log(f"flash {PORT}")
    cmd = [
        str(PY),
        "-m",
        "esptool",
        "--chip",
        "esp32c6",
        "-p",
        PORT,
        "-b",
        "460800",
        "write-flash",
        "--flash-size",
        "4MB",
        "0x0",
        str(BUILD / "bootloader" / "bootloader.bin"),
        "0x8000",
        str(BUILD / "partition_table" / "partition-table.bin"),
        "0x10000",
        str(BUILD / "temperature_sensor.bin"),
    ]
    r = subprocess.run(cmd, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "boot_wifi_val100_flash.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("flash failed")
    log("FLASH_OK — no further COM")


def progress() -> tuple[int, int, int]:
    text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.exists() else ""
    fulls = len(re.findall(r"^BWO_FULL ", text, re.M))
    hots = len(re.findall(r"^BWO V", text, re.M))
    finals = len(re.findall(r"^BWO_FINAL|BENCH_DONE boot_wifi_opt", text, re.M))
    return fulls, hots, finals


def wait_campaign(timeout_s: float = 25 * 60) -> None:
    log("wait VAL100 (no COM)")
    t0 = time.time()
    last = (-1, -1, -1)
    while time.time() - t0 < timeout_s:
        f, h, fin = progress()
        if (f, h, fin) != last:
            last = (f, h, fin)
            log(f"progress full={f} hot={h} final={fin}")
            text = RX_LOG.read_text(encoding="utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.startswith("BWO V")]
            if lines:
                log("  " + lines[-1][:220])
        if fin > 0 or h >= HOT_TARGET:
            log(f"STOP full={f} hot={h} final={fin}")
            return
        time.sleep(2.0)
    log(f"TIMEOUT full={last[0]} hot={last[1]} final={last[2]}")


def main() -> int:
    if PROGRESS.exists():
        PROGRESS.write_text("", encoding="utf-8")
    rebuild_receiver()
    start_receiver()
    cmake_configure()
    ninja_build()
    flash_once()
    wait_campaign()
    kill_receiver()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR {e}")
        kill_receiver()
        sys.exit(1)
