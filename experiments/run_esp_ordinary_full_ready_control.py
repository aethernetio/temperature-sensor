#!/usr/bin/env python3
"""Build/flash ESP ordinary FULL_READY control firmware (one message)."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_aether_hot_100x_2s_ppk_tcp_rx as hot  # noqa: E402

BUILD = ROOT / "build-esp-ordinary-full-ready-control"
FS_INIT = HERE / "preprovision" / "sender_fs_157aadbe.h"
OUT = HERE / "esp_ordinary_full_ready_control"
RUN_ID = 920004


def build_firmware(service_uid: str) -> Path:
    if not FS_INIT.is_file():
        raise RuntimeError(f"missing FS_INIT: {FS_INIT}")
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, _password = hot.wifi_creds()
    sdk_cfg = (BUILD / "sdkconfig").as_posix()
    defs = (
        f"-D SDKCONFIG='{sdk_cfg}' "
        f"-D SDKCONFIG_DEFAULTS='sdkconfig.defaults.esp_ordinary_full_ready_control' "
        f"-D AE_EXP_ORDINARY_FULL_READY_CONTROL=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D SERVICE_UID={service_uid} "
        f"-D AE_ORDINARY_FULL_READY_RUN_ID={RUN_ID} "
        f"-D FS_INIT={FS_INIT.as_posix()} "
        f"-B '{BUILD.as_posix()}'"
    )
    if not (BUILD / "CMakeCache.txt").is_file():
        r = hot.sb.run(hot.sb.idf_cmd(f"idf.py {defs} set-target esp32c6"), cwd=str(ROOT))
        if r.returncode != 0:
            raise RuntimeError("set-target failed")
    hot.sb.run(hot.sb.idf_cmd(f"idf.py {defs} reconfigure"), cwd=str(ROOT)).check_returncode()
    hot.BUILD = BUILD
    hot.ensure_aether_server_rebuild_method()
    hot.sb.run(hot.sb.idf_cmd(f"idf.py {defs} build"), cwd=str(ROOT)).check_returncode()
    hot.sync_flash_bin_from_elf()
    bin_path = BUILD / "temperature_sensor.bin"
    if not bin_path.is_file():
        raise RuntimeError("missing temperature_sensor.bin")
    return bin_path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def flash(port: str) -> None:
    defs = f"-B '{BUILD.as_posix()}'"
    cmd = hot.sb.idf_cmd(f"idf.py {defs} -p {port} flash")
    hot.sb.run(cmd, cwd=str(ROOT)).check_returncode()


def capture_serial(port: str, log_path: Path, seconds: int) -> None:
    import serial  # type: ignore

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with serial.Serial(port, 115200, timeout=0.2) as ser, log_path.open(
        "w", encoding="utf-8", errors="replace"
    ) as out:
        end = time.time() + seconds
        while time.time() < end:
            data = ser.read(4096)
            if data:
                text = data.decode("utf-8", errors="replace")
                out.write(text)
                out.flush()
            time.sleep(0.05)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--service-uid", required=True)
    p.add_argument("--port", default="COM4")
    p.add_argument("--serial-seconds", type=int, default=360)
    p.add_argument("--build-only", action="store_true")
    p.add_argument("--skip-flash", action="store_true")
    args = p.parse_args()
    env_path = HERE / "local_aether_wifi.env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    bin_path = build_firmware(args.service_uid)
    fw_sha = sha256_file(bin_path)
    print(f"FIRMWARE_SHA256={fw_sha}")
    print(f"FIRMWARE_PATH={bin_path}")
    if args.build_only:
        return 0
    if not args.skip_flash:
        flash(args.port)
    ts = time.strftime("%Y%m%d_%H%M%S")
    serial_log = OUT / f"serial_{ts}.log"
    capture_serial(args.port, serial_log, args.serial_seconds)
    print(f"SERIAL_LOG={serial_log}")
    text = serial_log.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"FULL_READY_WRITE_TERMINAL status=(\w+)", text)
    print(f"WRITE_TERMINAL={m.group(1) if m else 'missing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
