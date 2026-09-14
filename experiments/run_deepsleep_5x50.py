"""Build/flash/monitor prepared deep-sleep 5x50 E2E (silent ESP, Æther receiver)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
# Reuse existing IDF-configured ESP32-C6 build tree (do not create a desktop cmake dir).
BUILD = ROOT / "build-esp32c6-save-bench-smoke"
AETHER = r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0"
PY = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
CMAKE = Path(r"C:\Espressif\tools\cmake\3.30.2\bin\cmake.exe")
NINJA = Path(r"C:\Espressif\tools\ninja\1.12.1\ninja.exe")
RX_EXE = ROOT / "temperature_receiver" / "build-bisect" / "temperature_receiver.exe"
RX_SESSION = ROOT / "experiments" / "prepared_wifi_cache_rx_session"
RX_LOG = ROOT / "experiments" / "prepared_deepsleep_5x50_rx.log"
TSV = ROOT / "experiments" / "prepared_deepsleep_5x50.tsv"
PROGRESS = ROOT / "experiments" / "deepsleep_5x50_progress.log"
CHAT = ROOT / "experiments" / "deepsleep_5x50_chat.txt"

IDF_PATH = r"C:\Espressif\frameworks\esp-idf-v6.0.2"
CCACHE = r"C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64"

RESULT_RE = re.compile(r"TEST_RESULT .* BENCH_DONE deepsleep_5x50|BENCH_DONE deepsleep_5x50")
OUTER_RE = re.compile(r"\[OUTER (?P<o>\d+)/5\]")


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


def show_effective() -> None:
    sdk = BUILD / "sdkconfig"
    keys = [
        "CONFIG_RTC_CLK_SRC_EXT_CRYS",
        "CONFIG_RTC_CLK_SRC_INT_RC",
        "CONFIG_ESP_BROWNOUT_DET",
        "CONFIG_ESP_BROWNOUT_DET_LVL_SEL_7",
        "CONFIG_ESP_WIFI_ENABLE_WPA3_SAE",
        "CONFIG_ESP_CONSOLE_NONE",
        "CONFIG_LOG_DEFAULT_LEVEL_NONE",
        "CONFIG_BOOTLOADER_LOG_LEVEL_NONE",
        "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160",
        "CONFIG_PM_ENABLE",
        "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP",
        "CONFIG_RTC_CLK_CAL_CYCLES",
    ]
    log("=== effective sdkconfig ===")
    text = sdk.read_text(encoding="utf-8") if sdk.exists() else ""
    for k in keys:
        lines = [ln for ln in text.splitlines() if k in ln and not ln.strip().startswith("# ") or ln.startswith(f"# {k}")]
        # simpler: grep style
        matched = [ln for ln in text.splitlines() if k in ln]
        for ln in matched[:3]:
            log(ln)


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
        "-DAE_EXP_PREPARED_DEEPSLEEP_5X50=1",
        "-DAE_EXP_PREPARED_WIFI_FASTEST=",
        "-DAE_EXP_PREPARED_WIFI_BISECT=",
        "-DAE_EXP_BISECT_CONSOLE=",
        "-DAE_EXP_BISECT_SMOKE=",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DSERVICE_UID=5aade50f-00d9-4624-b097-e203cdcf1e38",
        "-DBENCH_CLIENT_ID=prepared_deepsleep_5x50_v1",
        "-DWIFI_SSID=chirkov",
        "-DWIFI_PASSWORD=kcdjepWz51",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    log("cmake configure deepsleep_5x50")
    r = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "deepsleep_cmake.err").write_text(
            r.stdout + "\n" + r.stderr, encoding="utf-8"
        )
        raise RuntimeError("cmake failed")
    log("cmake ok")


def ninja_build() -> None:
    r = subprocess.run(
        [str(NINJA), "-C", str(BUILD)], env=env(), capture_output=True, text=True
    )
    if r.returncode != 0:
        (ROOT / "experiments" / "deepsleep_build.err").write_text(
            r.stdout[-12000:] + "\n" + r.stderr[-12000:], encoding="utf-8"
        )
        raise RuntimeError("ninja failed")
    log("build ok")


def flash() -> None:
    cmd = [
        str(PY),
        "-m",
        "esptool",
        "--chip",
        "esp32c6",
        "-p",
        "COM7",
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
        (ROOT / "experiments" / "deepsleep_flash.err").write_text(
            r.stdout + "\n" + r.stderr, encoding="utf-8"
        )
        raise RuntimeError("flash failed")
    log("flash ok")


def ensure_receiver() -> None:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq temperature_receiver.exe"],
        capture_output=True,
        text=True,
    ).stdout
    if "temperature_receiver.exe" in out:
        log("receiver already running")
        return
    RX_SESSION.mkdir(parents=True, exist_ok=True)
    env2 = env()
    env2["AE_RECEIVER_SESSION_DIR"] = str(RX_SESSION)
    env2["AE_DS_TSV"] = str(TSV)
    RX_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RX_LOG.open("a", encoding="utf-8") as outf, (
        ROOT / "experiments" / "prepared_deepsleep_5x50_rx.log.err"
    ).open("a", encoding="utf-8") as errf:
        subprocess.Popen(
            [str(RX_EXE)],
            cwd=str(RX_SESSION),
            env=env2,
            stdout=outf,
            stderr=errf,
        )
    time.sleep(4)
    log("receiver started")


def tsv_stats() -> dict:
    if not TSV.exists():
        return {}
    rows = list(TSV.read_text(encoding="utf-8").splitlines())
    if len(rows) < 2:
        return {}
    full = hot = 0
    outers_full = set()
    for line in rows[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        kind = parts[1]
        outer = parts[2]
        if kind == "1":
            full += 1
            outers_full.add(outer)
        elif kind == "2":
            hot += 1
    return {"full": full, "hot": hot, "outers": outers_full}


def wait_done(timeout_s: int = 1800) -> str:
    deadline = time.time() + timeout_s
    last_len = 0
    last_outer = 0
    while time.time() < deadline:
        if RX_LOG.exists():
            text = RX_LOG.read_text(encoding="utf-8", errors="replace")
            if len(text) != last_len:
                last_len = len(text)
                for m in OUTER_RE.finditer(text):
                    o = int(m.group("o"))
                    if o > last_outer:
                        last_outer = o
                        idx = m.start()
                        block = text[idx : idx + 350]
                        with CHAT.open("a", encoding="utf-8") as f:
                            f.write(block + "\n")
                        print(block, flush=True)
                if "BENCH_DONE deepsleep_5x50" in text:
                    for line in reversed(text.splitlines()):
                        if line.startswith("TEST_RESULT"):
                            return line
                    return "BENCH_DONE"
        st = tsv_stats()
        # Complete enough without FINAL: 5 FULL + >=245 HOT
        if st.get("full", 0) >= 5 and st.get("hot", 0) >= 245:
            log(f"TSV complete enough full={st['full']} hot={st['hot']}")
            return f"TSV_COMPLETE full={st['full']} hot={st['hot']}"
        time.sleep(5)
    raise TimeoutError("no BENCH_DONE")


def main() -> int:
    CHAT.write_text("", encoding="utf-8")
    if TSV.exists():
        TSV.unlink()
    ensure_receiver()
    cmake_configure()
    force_sdk_fixes()
    show_effective()
    ninja_build()
    force_sdk_fixes()
    show_effective()
    flash()
    log("waiting for 5x50 deepsleep run (~15-25 min)...")
    result = wait_done(2400)
    log("RESULT " + result)
    with CHAT.open("a", encoding="utf-8") as f:
        f.write(result + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
