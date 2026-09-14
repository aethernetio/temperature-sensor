#!/usr/bin/env python3
"""Build+flash UDP RTC index with Thermometer-2 peripheral power-down (no erase, no PPK)."""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
sys.path.insert(0, str(ROOT / "experiments"))
import run_prepared_power_factor_study as pf  # noqa: E402

BUILD = ROOT / "build-udp-rtc-t2-pwroff"
UDP_PORT = 9000
# BOARD_AETHER_ESP32_C6 == 0 in user_config.h / board headers @ ef5da3b
BOARD_AETHER_ESP32_C6 = 0


def local_ipv4() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getpeername()[0] if False else s.getsockname()[0]
    finally:
        s.close()


def force_sdk(sdk: Path) -> None:
    """Silent low-power profile + ULP HP stop API (no ULP app load/run)."""
    text = sdk.read_text(encoding="utf-8", errors="replace")
    b = pf._set_kconfig_bool

    # Enable ULP component so ulp_lp_core_stop() links; CMake skips ulp_add_project.
    text = b(text, "CONFIG_ULP_COPROC_ENABLED", True)
    text = b(text, "CONFIG_ULP_COPROC_TYPE_LP_CORE", True)
    text = b(text, "CONFIG_ULP_COPROC_TYPE_FSM", False)
    text = b(text, "CONFIG_ULP_COPROC_TYPE_RISCV", False)

    text = b(text, "CONFIG_COMPILER_OPTIMIZATION_SIZE", True)
    text = b(text, "CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_DISABLE", True)

    text = b(text, "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP", True)
    text = b(text, "CONFIG_BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON", True)
    for lvl in ("NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"):
        text = b(text, f"CONFIG_BOOTLOADER_LOG_LEVEL_{lvl}", lvl == "NONE")

    for lvl in ("NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"):
        text = b(text, f"CONFIG_LOG_DEFAULT_LEVEL_{lvl}", lvl == "NONE")
    text = b(text, "CONFIG_LOG_MAXIMUM_EQUALS_DEFAULT", True)
    for lvl in ("ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"):
        text = b(text, f"CONFIG_LOG_MAXIMUM_LEVEL_{lvl}", False)

    text = b(text, "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG", False)
    text = b(text, "CONFIG_ESP_CONSOLE_NONE", False)
    text = b(text, "CONFIG_ESP_CONSOLE_UART_DEFAULT", True)
    text = b(text, "CONFIG_ESP_CONSOLE_SECONDARY_NONE", True)

    text = b(text, "CONFIG_ESP_WIFI_AMPDU_TX_ENABLED", False)
    text = b(text, "CONFIG_ESP_WIFI_AMPDU_RX_ENABLED", False)
    text = b(text, "CONFIG_ESP_WIFI_NVS_ENABLED", False)
    text = b(text, "CONFIG_ESP_PHY_RF_CAL_PARTIAL", True)
    text = b(text, "CONFIG_ESP_PHY_RF_CAL_NONE", False)
    text = b(text, "CONFIG_ESP_PHY_RF_CAL_FULL", False)

    text = b(text, "CONFIG_LWIP_ESP_GRATUITOUS_ARP", False)
    text = b(text, "CONFIG_ESP_GRATUITOUS_ARP", False)

    text = b(text, "CONFIG_PM_ENABLE", True)
    text = b(text, "CONFIG_FREERTOS_USE_TICKLESS_IDLE", True)

    text = re.sub(
        r"^CONFIG_LOG_DEFAULT_LEVEL=\d+\s*$",
        "CONFIG_LOG_DEFAULT_LEVEL=0",
        text,
        flags=re.M,
    )
    sdk.write_text(text, encoding="utf-8")


def main() -> int:
    print(
        "NOTE: Thermometer 2 membership is not labeled in local repo docs; "
        "using BOARD_AETHER_ESP32_C6 pinout @ ef5da3b per user designation.",
        flush=True,
    )
    host = local_ipv4()
    print(f"UDP server host={host} port={UDP_PORT}", flush=True)
    print(f"BOARD={BOARD_AETHER_ESP32_C6} (BOARD_AETHER_ESP32_C6)", flush=True)

    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    pf.camp.BUILD = BUILD
    pf.BUILD = BUILD

    wifi = pf.camp.APS["chirkov"]
    args = [
        str(pf.camp.CMAKE),
        "-S",
        str(ROOT),
        "-B",
        str(BUILD),
        "-G",
        "Ninja",
        "-DCMAKE_SUPPRESS_REGENERATION=ON",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        f"-DCMAKE_TOOLCHAIN_FILE={pf.camp.TOOLCHAIN.as_posix()}",
        "-DIDF_TARGET=esp32c6",
        f"-DCPM_aether-client-cpp_SOURCE={pf.camp.AETHER}",
        f"-DUSER_CONFIG={(ROOT / 'main' / 'user_config.h').as_posix()}",
        f"-DFS_INIT={pf.camp.FS_INIT.as_posix()}",
        f"-DSDKCONFIG={BUILD.as_posix()}/sdkconfig",
        "-DAE_DISTILLATION=ON",
        "-DAE_FILTRATION=ON",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DBOARD={BOARD_AETHER_ESP32_C6}",
        f"-DWIFI_SSID={wifi['ssid']}",
        f"-DWIFI_PASSWORD={wifi['password']}",
        f"-DSERVICE_UID={pf.camp.SERVICE_UID}",
        f"-DAE_UDP_SERVER_HOST={host}",
        f"-DAE_UDP_SERVER_PORT={UDP_PORT}",
        "-DAETHER_DIAG_UDP_RTC_INDEX=1",
        f"-DPython3_EXECUTABLE={pf.camp.PY.as_posix()}",
        f"-DCMAKE_MAKE_PROGRAM={pf.camp.NINJA.as_posix()}",
        f"-DCMAKE_C_COMPILER={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-gcc.exe').as_posix()}",
        f"-DCMAKE_CXX_COMPILER={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-g++.exe').as_posix()}",
        f"-DCMAKE_OBJCOPY={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-objcopy.exe').as_posix()}",
    ]
    args.extend(pf.clear_power_exp_flags({"AETHER_DIAG_UDP_RTC_INDEX": "1"}))

    print("cmake configure", flush=True)
    pf.camp.clean_ninja_logs()
    for i, label in enumerate(("cmake1", "cmake2", "cmake3"), start=1):
        force_sdk(BUILD / "sdkconfig")
        r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
        (BUILD / f"{label}.log").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        if r.returncode != 0:
            print(r.stderr[-4000:] if r.stderr else r.stdout[-4000:])
            raise SystemExit(f"{label} failed rc={r.returncode}")
    force_sdk(BUILD / "sdkconfig")
    # Final cmake so sdkconfig.h matches forced ULP enable.
    r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    (BUILD / "cmake4.log").write_text(
        (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
    )
    if r.returncode != 0:
        print(r.stderr[-4000:] if r.stderr else r.stdout[-4000:])
        raise SystemExit(f"cmake4 failed rc={r.returncode}")

    sdk_text = (BUILD / "sdkconfig").read_text(encoding="utf-8", errors="replace")
    h_text = (BUILD / "config" / "sdkconfig.h").read_text(encoding="utf-8", errors="replace")
    ulp_on = "CONFIG_ULP_COPROC_ENABLED=y" in sdk_text
    ulp_h = "#define CONFIG_ULP_COPROC_ENABLED 1" in h_text
    print(f"ULP check: sdk_enabled={ulp_on} h_enabled={ulp_h}", flush=True)
    if not ulp_on or not ulp_h:
        raise SystemExit("CONFIG_ULP_COPROC_ENABLED not enabled (needed for ulp_lp_core_stop)")

    print("ninja", flush=True)
    e = pf.camp.env()
    e["PATH"] = str(pf.camp.NINJA.parent) + ";" + e.get("PATH", "")
    n = subprocess.run([str(pf.camp.NINJA), "-C", str(BUILD)], cwd=ROOT, env=e)
    if n.returncode != 0:
        raise SystemExit(f"ninja failed rc={n.returncode}")

    # Verify symbols / board defs in object dump briefly via strings on elf
    elf = BUILD / "temperature_sensor.elf"
    print(f"elf={elf} size={elf.stat().st_size}", flush=True)

    # Prefer write-flash WITHOUT erase (preserve NVS).
    print(pf.camp.flash(erase=False), flush=True)
    print("flash done (no erase, no serial monitor, no PPK)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
