"""Build/flash TXD4 FULL-loop diag; capture <=3 FULL then HOT1..3."""

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
PROGRESS = ROOT / "experiments" / "full_loop_diag_progress.log"
RX_LOG = ROOT / "experiments" / "full_loop_diag_rx.log"
TSV = ROOT / "experiments" / "full_loop_diag.tsv"
PORT = "COM7"


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
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
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
        (ROOT / "experiments" / "full_loop_rx_build.err").write_text(
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
        ROOT / "experiments" / "full_loop_diag_rx.log.err"
    ).open("w", encoding="utf-8") as errf:
        subprocess.Popen(
            [str(RX_EXE)],
            cwd=str(RX_SESSION),
            env=env2,
            stdout=outf,
            stderr=errf,
        )
    time.sleep(4)
    log("receiver started")


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
        "-DAE_EXP_PREPARED_TX_DONE_DIAG=1",
        "-DAE_EXP_TX_DIAG_MODE=0",
        "-DAE_EXP_PREPARED_DEEPSLEEP_5X50=",
        "-DAE_EXP_PREPARED_WIFI_FASTEST=",
        "-DAE_EXP_PREPARED_WIFI_BISECT=",
        "-DAE_EXP_BISECT_CONSOLE=",
        "-DAE_EXP_BISECT_SMOKE=",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DSERVICE_UID=5aade50f-00d9-4624-b097-e203cdcf1e38",
        "-DBENCH_CLIENT_ID=prepared_deepsleep_5x50_v1",
        "-DAETHER_PREPARED_NONCE_RESERVE=60",
        "-DWIFI_SSID=chirkov",
        "-DWIFI_PASSWORD=kcdjepWz51",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    log("cmake configure full_loop_diag")
    r = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "full_loop_cmake.err").write_text(
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
        (ROOT / "experiments" / "full_loop_build.err").write_text(
            (r.stdout or "")[-12000:] + "\n" + (r.stderr or "")[-12000:],
            encoding="utf-8",
        )
        raise RuntimeError("ninja failed")
    log("build ok")


def flash() -> None:
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
        (ROOT / "experiments" / "full_loop_flash.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError("flash failed")
    log("FLASH_OK")


def parse_counts(text: str) -> tuple[int, int, list[str], list[str]]:
    fulls = re.findall(r"^FULL_DIAG .+$", text, re.M)
    hots = re.findall(r"^RECV HOT .+$", text, re.M)
    return len(fulls), len(hots), fulls, hots


def wait_capture(max_full: int = 3, need_hot: int = 3, timeout_s: float = 180.0) -> None:
    log(
        f"wait capture max_full={max_full} need_hot={need_hot} "
        "(power-cycle board once if needed after flash)"
    )
    t0 = time.time()
    last_full = 0
    while time.time() - t0 < timeout_s:
        text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.exists() else ""
        nf, nh, fulls, hots = parse_counts(text)
        if nf != last_full:
            last_full = nf
            log(f"progress full={nf} hot={nh}")
            if fulls:
                log("  " + fulls[-1][:240])
        if nh >= need_hot:
            log(f"STOP ok FULL={nf} HOT={nh}")
            for line in fulls[:3]:
                log("  " + line[:240])
            for line in hots[:3]:
                log("  " + line[:240])
            return
        if nf >= max_full and nh == 0:
            log(f"STOP at {nf} FULL with HOT=0 (diag only)")
            for line in fulls[:3]:
                log("  " + line[:240])
            return
        time.sleep(1.0)
    text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.exists() else ""
    nf, nh, fulls, hots = parse_counts(text)
    log(f"TIMEOUT FULL={nf} HOT={nh}")
    for line in fulls[:3]:
        log("  " + line[:240])
    for line in hots[:3]:
        log("  " + line[:240])


def main() -> int:
    if PROGRESS.exists():
        PROGRESS.write_text("", encoding="utf-8")
    rebuild_receiver()
    start_receiver()
    cmake_configure()
    ninja_build()
    flash()
    wait_capture()
    kill_receiver()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR {e}")
        kill_receiver()
        sys.exit(1)
