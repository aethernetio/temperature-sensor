"""
One-flash boot/Wi-Fi HOT opt campaign orchestrator.
COM only for flash; after FLASH_OK never touch serial.
Progress = receiver TSV/log only.
"""

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
PROGRESS = ROOT / "experiments" / "boot_wifi_opt_progress.log"
RX_LOG = ROOT / "experiments" / "prepared_boot_wifi_opt_rx.log"
TSV = ROOT / "experiments" / "prepared_boot_wifi_opt.tsv"
PORT = "COM7"
VARIANTS = 11
HOT_PER = 30


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
        ("CONFIG_ESP32_WIFI_ENABLE_WPA3_SAE=y", "# CONFIG_ESP32_WIFI_ENABLE_WPA3_SAE is not set"),
        ("CONFIG_RTC_CLK_SRC_INT_RC=y", "# CONFIG_RTC_CLK_SRC_INT_RC is not set"),
        ("# CONFIG_RTC_CLK_SRC_EXT_CRYS is not set", "CONFIG_RTC_CLK_SRC_EXT_CRYS=y"),
        ("CONFIG_ESP_BROWNOUT_DET=n", "CONFIG_ESP_BROWNOUT_DET=y"),
        ("# CONFIG_ESP_BROWNOUT_DET is not set", "CONFIG_ESP_BROWNOUT_DET=y"),
        ("CONFIG_PM_ENABLE=y", "# CONFIG_PM_ENABLE is not set"),
    ]
    for a, b in reps:
        text = text.replace(a, b)
    if "CONFIG_RTC_CLK_SRC_EXT_CRYS=y" not in text:
        text += "\nCONFIG_RTC_CLK_SRC_EXT_CRYS=y\n"
    # A4 CONFIG_RTC_CLK_CAL_CYCLES=0: first flash with forced 0 left COM awake
    # and produced zero Aether telemetry; keep IDF effective default (1024).
    if "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP=y" not in text:
        text += "\nCONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP=y\n"
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
        (ROOT / "experiments" / "boot_wifi_opt_rx_build.err").write_text(
            (r.stdout or "")[-8000:] + "\n" + (r.stderr or "")[-8000:],
            encoding="utf-8",
        )
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
        ROOT / "experiments" / "prepared_boot_wifi_opt_rx.log.err"
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
        "-DAE_EXP_PREPARED_BOOT_WIFI_OPT=1",
        "-DAE_EXP_PREPARED_MAC_RETRY_DIAG=",
        "-DAE_EXP_PREPARED_TX_DONE_DIAG=",
        "-DAE_EXP_PREPARED_DEEPSLEEP_5X50=",
        "-DAE_EXP_PREPARED_WIFI_FASTEST=",
        "-DAE_EXP_PREPARED_WIFI_BISECT=",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DSERVICE_UID=5aade50f-00d9-4624-b097-e203cdcf1e38",
        "-DBENCH_CLIENT_ID=prepared_deepsleep_5x50_v1",
        "-DAETHER_PREPARED_NONCE_RESERVE=40",
        "-DWIFI_SSID=chirkov",
        "-DWIFI_PASSWORD=kcdjepWz51",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    log("cmake configure boot_wifi_opt")
    r = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "boot_wifi_opt_cmake.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("cmake failed")
    force_sdk_fixes()
    # Re-run cmake so Kconfig picks forced sdkconfig values when needed.
    r2 = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r2.returncode != 0:
        raise RuntimeError("cmake reconfigure failed")
    force_sdk_fixes()
    log("cmake ok")


def ninja_build() -> None:
    log("ninja build")
    r = subprocess.run(
        [str(NINJA), "-C", str(BUILD)], env=env(), capture_output=True, text=True
    )
    if r.returncode != 0:
        (ROOT / "experiments" / "boot_wifi_opt_build.err").write_text(
            (r.stdout or "")[-16000:] + "\n" + (r.stderr or "")[-8000:],
            encoding="utf-8",
        )
        raise RuntimeError("ninja failed")
    log("build ok")
    # Snapshot effective boot/rtc flags
    sdk = BUILD / "sdkconfig"
    keys = [
        "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP",
        "CONFIG_BOOTLOADER_SKIP_VALIDATE_ALWAYS",
        "CONFIG_BOOTLOADER_COMPILER_OPTIMIZATION_PERF",
        "CONFIG_BOOTLOADER_COMPILER_OPTIMIZATION_SIZE",
        "CONFIG_ESPTOOLPY_FLASHMODE",
        "CONFIG_ESPTOOLPY_FLASHFREQ",
        "CONFIG_RTC_CLK_SRC_EXT_CRYS",
        "CONFIG_RTC_CLK_CAL_CYCLES",
        "CONFIG_SECURE_BOOT",
        "CONFIG_FLASH_ENCRYPTION_ENABLED",
        "CONFIG_ESP_WIFI_AMPDU_TX_ENABLED",
        "CONFIG_ESP_WIFI_AMPDU_RX_ENABLED",
        "CONFIG_ESP_WIFI_STATIC_RX_BUFFER_NUM",
        "CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM",
        "CONFIG_ESP_WIFI_DYNAMIC_TX_BUFFER_NUM",
    ]
    lines = []
    text = sdk.read_text(encoding="utf-8", errors="replace") if sdk.exists() else ""
    for k in keys:
        for ln in text.splitlines():
            if k in ln and not ln.strip().startswith("#") or ln.startswith(f"# {k}"):
                if k in ln:
                    lines.append(ln)
                    break
    (ROOT / "experiments" / "boot_wifi_opt_sdkconfig_snapshot.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def wait_com_for_flash_only(timeout_s: float = 120.0) -> None:
    log(f"pre-flash: waiting up to {int(timeout_s)}s for {PORT} (awake window)")
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
    raise RuntimeError(f"{PORT} not available for flash — wake/power-cycle ESP once")


def flash_once() -> None:
    wait_com_for_flash_only()
    log(f"flash {PORT} (COM allowed only here)")
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
        (ROOT / "experiments" / "boot_wifi_opt_flash.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("flash failed")
    log("FLASH_OK — closing COM; further progress via Aether only")


def progress_from_log() -> tuple[int, int, int]:
    text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.exists() else ""
    fulls = len(re.findall(r"^BWO_FULL ", text, re.M))
    hots = len(re.findall(r"^BWO V", text, re.M))
    finals = len(re.findall(r"^BWO_FINAL|BENCH_DONE boot_wifi_opt", text, re.M))
    return fulls, hots, finals


def wait_campaign(timeout_s: float = 55 * 60) -> None:
    log("wait Aether campaign (no COM)")
    t0 = time.time()
    last = (-1, -1, -1)
    while time.time() - t0 < timeout_s:
        f, h, fin = progress_from_log()
        if (f, h, fin) != last:
            last = (f, h, fin)
            log(f"progress full={f} hot={h} final={fin}")
            text = RX_LOG.read_text(encoding="utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.startswith("BWO V")]
            if lines:
                log("  " + lines[-1][:220])
        if fin > 0 or h >= VARIANTS * HOT_PER:
            log(f"STOP campaign full={f} hot={h} final={fin}")
            return
        if f >= VARIANTS + 1 and h >= VARIANTS * HOT_PER - 5:
            log(f"STOP near-complete full={f} hot={h}")
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
