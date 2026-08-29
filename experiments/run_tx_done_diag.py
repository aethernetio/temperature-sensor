"""Build/flash/monitor TX-done diagnostic runs A (FIRST_ANY) and B (FIRST_SUCCESS)."""

from __future__ import annotations

import os
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

PROGRESS = ROOT / "experiments" / "tx_done_diag_progress.log"


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


def rebuild_receiver() -> None:
    log("rebuild temperature_receiver")
    r = subprocess.run(
        [str(CMAKE), "--build", str(RX_BUILD), "--parallel"],
        env=env(),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        (ROOT / "experiments" / "tx_diag_rx_build.err").write_text(
            r.stdout[-8000:] + "\n" + r.stderr[-8000:], encoding="utf-8"
        )
        raise RuntimeError("receiver build failed")
    log("receiver build ok")


def kill_receiver() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "temperature_receiver.exe"],
        capture_output=True,
        text=True,
    )
    time.sleep(1)


def start_receiver(tsv: Path, rx_log: Path) -> None:
    kill_receiver()
    RX_SESSION.mkdir(parents=True, exist_ok=True)
    env2 = env()
    env2["AE_RECEIVER_SESSION_DIR"] = str(RX_SESSION)
    env2["AE_DS_TSV"] = str(tsv)
    with rx_log.open("w", encoding="utf-8") as outf, (
        ROOT / "experiments" / "prepared_tx_done_diag_rx.log.err"
    ).open("w", encoding="utf-8") as errf:
        subprocess.Popen(
            [str(RX_EXE)],
            cwd=str(RX_SESSION),
            env=env2,
            stdout=outf,
            stderr=errf,
        )
    time.sleep(4)
    log(f"receiver started tsv={tsv.name}")


def cmake_configure(mode: int) -> None:
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
        f"-DAE_EXP_TX_DIAG_MODE={mode}",
        "-DAE_EXP_PREPARED_DEEPSLEEP_5X50=",
        "-DAE_EXP_PREPARED_WIFI_FASTEST=",
        "-DAE_EXP_PREPARED_WIFI_BISECT=",
        "-DAE_EXP_BISECT_CONSOLE=",
        "-DAE_EXP_BISECT_SMOKE=",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DSERVICE_UID=5aade50f-00d9-4624-b097-e203cdcf1e38",
        f"-DBENCH_CLIENT_ID=prepared_deepsleep_5x50_v1",
        "-DAETHER_PREPARED_NONCE_RESERVE=60",
        "-DWIFI_SSID=chirkov",
        "-DWIFI_PASSWORD=kcdjepWz51",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    log(f"cmake configure tx_done_diag mode={mode}")
    r = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (ROOT / "experiments" / "tx_diag_cmake.err").write_text(
            r.stdout + "\n" + r.stderr, encoding="utf-8"
        )
        raise RuntimeError("cmake failed")
    log("cmake ok")


def ninja_build() -> None:
    r = subprocess.run(
        [str(NINJA), "-C", str(BUILD)], env=env(), capture_output=True, text=True
    )
    if r.returncode != 0:
        (ROOT / "experiments" / "tx_diag_build.err").write_text(
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
        (ROOT / "experiments" / "tx_diag_flash.err").write_text(
            r.stdout + "\n" + r.stderr, encoding="utf-8"
        )
        raise RuntimeError("flash failed")
    log("flash ok")


def tsv_stats(tsv: Path) -> dict:
    if not tsv.exists():
        return {}
    rows = list(tsv.read_text(encoding="utf-8").splitlines())
    if len(rows) < 2:
        return {}
    full = hot = hot_diag = 0
    for line in rows[1:]:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        if parts[1] == "1":
            # Prefer outer==1 campaign rows when present
            if len(parts) > 2 and parts[2] == "1":
                full += 1
            elif len(parts) <= 2:
                full += 1
        elif parts[1] == "2":
            hot += 1
            # diag rows have first_status != 255 (col 21) or tx_cb_total > 0
            if len(parts) > 21:
                try:
                    first_st = int(parts[21])
                    cb_total = int(parts[18]) if len(parts) > 18 else 0
                except ValueError:
                    first_st, cb_total = 255, 0
                if first_st != 255 or cb_total > 0:
                    hot_diag += 1
    return {"full": full, "hot": hot, "hot_diag": hot_diag}


def wait_done(tsv: Path, rx_log: Path, timeout_s: int = 900) -> str:
    deadline = time.time() + timeout_s
    last_hot = -1
    while time.time() < deadline:
        if rx_log.exists():
            text = rx_log.read_text(encoding="utf-8", errors="replace")
            if "BENCH_DONE tx_done_diag" in text:
                for line in reversed(text.splitlines()):
                    if line.startswith("TEST_RESULT"):
                        return line
                return "BENCH_DONE"
        st = tsv_stats(tsv)
        hot_diag = st.get("hot_diag", 0)
        if hot_diag != last_hot:
            last_hot = hot_diag
            log(
                f"progress full={st.get('full', 0)} hot={st.get('hot', 0)} "
                f"hot_diag={hot_diag}"
            )
        if st.get("full", 0) >= 1 and hot_diag >= 48:
            time.sleep(8)
            st2 = tsv_stats(tsv)
            if st2.get("hot_diag", 0) >= 48:
                log(
                    f"TSV complete enough full={st2['full']} "
                    f"hot_diag={st2['hot_diag']}"
                )
                return (
                    f"TSV_COMPLETE full={st2['full']} "
                    f"hot_diag={st2['hot_diag']}"
                )
        time.sleep(5)
    raise TimeoutError("no BENCH_DONE / incomplete TSV")


def run_mode(mode: int, label: str) -> str:
    tsv = ROOT / "experiments" / f"prepared_tx_done_diag_{label}.tsv"
    rx_log = ROOT / "experiments" / f"prepared_tx_done_diag_{label}_rx.log"
    if tsv.exists():
        tsv.unlink()
    start_receiver(tsv, rx_log)
    cmake_configure(mode)
    force_sdk_fixes()
    ninja_build()
    force_sdk_fixes()
    flash()
    log(f"waiting for mode {label} (~4-8 min)...")
    result = wait_done(tsv, rx_log, 1200)
    log(f"RESULT {label}: {result}")
    return result


def main() -> int:
    PROGRESS.write_text("", encoding="utf-8")
    rebuild_receiver()
    # MODE A FIRST_ANY (0), then MODE B FIRST_SUCCESS (1)
    ra = run_mode(0, "A_FIRST_ANY")
    # brief gap so AP forgets STA
    time.sleep(8)
    rb = run_mode(1, "B_FIRST_SUCCESS")
    log(f"ALL_DONE A={ra} B={rb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
