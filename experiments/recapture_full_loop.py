"""Restart receiver, hard-reset ESP, wait for FULL_DIAG / HOT."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
PY = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
RX_EXE = ROOT / "temperature_receiver" / "build-bisect" / "temperature_receiver.exe"
RX_SESSION = ROOT / "experiments" / "prepared_wifi_cache_rx_session"
RX_LOG = ROOT / "experiments" / "full_loop_diag_rx.log"
TSV = ROOT / "experiments" / "full_loop_diag.tsv"
PROGRESS = ROOT / "experiments" / "full_loop_diag_progress.log"
PORT = "COM7"
CCACHE = r"C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64"


def env() -> dict:
    e = os.environ.copy()
    e["IDF_PATH"] = r"C:\Espressif\frameworks\esp-idf-v6.0.2"
    e["IDF_TOOLS_PATH"] = r"C:\Espressif"
    e["Path"] = (
        CCACHE
        + r";C:\Espressif\tools\ninja\1.12.1;C:\Espressif\tools\cmake\3.30.2\bin;C:\msys64\ucrt64\bin;"
        + e.get("Path", "")
    )
    return e


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def kill_receiver() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "temperature_receiver.exe"],
        capture_output=True,
        text=True,
    )
    time.sleep(2)


def start_receiver() -> None:
    kill_receiver()
    if TSV.exists():
        TSV.unlink()
    RX_SESSION.mkdir(parents=True, exist_ok=True)
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
    time.sleep(8)
    log("receiver restarted")


def hard_reset() -> None:
    # esptool hard_reset via chip_id (cheap) then flash stub exit
    cmd = [
        str(PY),
        "-m",
        "esptool",
        "--chip",
        "esp32c6",
        "-p",
        PORT,
        "run",
    ]
    r = subprocess.run(cmd, env=env(), capture_output=True, text=True)
    log(f"esptool run rc={r.returncode}")
    if r.returncode != 0:
        # fallback: chip_id triggers reset on many boards
        r2 = subprocess.run(
            [
                str(PY),
                "-m",
                "esptool",
                "--chip",
                "esp32c6",
                "-p",
                PORT,
                "chip-id",
            ],
            env=env(),
            capture_output=True,
            text=True,
        )
        log(f"esptool chip-id rc={r2.returncode}")


def wait_capture(timeout_s: float = 240.0) -> None:
    t0 = time.time()
    last = (0, 0)
    while time.time() - t0 < timeout_s:
        text = RX_LOG.read_text(encoding="utf-8", errors="replace") if RX_LOG.exists() else ""
        fulls = re.findall(r"^FULL_DIAG .+$", text, re.M)
        hots = re.findall(r"^RECV HOT .+$", text, re.M)
        nf, nh = len(fulls), len(hots)
        if (nf, nh) != last:
            last = (nf, nh)
            log(f"progress full={nf} hot={nh}")
            if fulls:
                log("  " + fulls[-1][:260])
            if hots:
                log("  " + hots[-1][:200])
        if nh >= 3:
            log(f"STOP ok FULL={nf} HOT={nh}")
            return
        if nf >= 3 and nh == 0:
            log(f"STOP diag-only FULL={nf}")
            return
        # also stop after 1 FULL with clear reason if still no HOT after another ~20s
        time.sleep(1.0)
    log(f"TIMEOUT FULL={last[0]} HOT={last[1]}")


def main() -> None:
    log("=== recapture after hard reset ===")
    start_receiver()
    # Give receiver time to reach cloud before ESP boots/sends
    time.sleep(10)
    hard_reset()
    wait_capture()


if __name__ == "__main__":
    main()
