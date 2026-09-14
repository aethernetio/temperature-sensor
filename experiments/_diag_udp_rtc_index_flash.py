#!/usr/bin/env python3
"""Build+flash AETHER_DIAG_UDP_RTC_INDEX (WiFi UDP index, 1s deep sleep)."""
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

BUILD = ROOT / "build-udp-rtc-index"
UDP_PORT = 9000


def local_ipv4() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _set_kconfig_value(text: str, symbol: str, value: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip().rstrip("\r")
        if stripped.startswith(f"{symbol}="):
            continue
        lines.append(line)
    lines.append(f"{symbol}={value}")
    return "\n".join(lines) + "\n"


def force_sdk(sdk: Path) -> None:
    """Low-power sdkconfig aligned with the ~35 mC reference (IDF 6-safe)."""
    text = sdk.read_text(encoding="utf-8", errors="replace")
    b = pf._set_kconfig_bool

    text = b(text, "CONFIG_ULP_COPROC_ENABLED", False)

    # Compiler
    text = b(text, "CONFIG_COMPILER_OPTIMIZATION_SIZE", True)
    text = b(text, "CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_DISABLE", True)

    # Bootloader
    text = b(text, "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP", True)
    text = b(text, "CONFIG_BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON", True)
    for lvl in ("NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"):
        text = b(text, f"CONFIG_BOOTLOADER_LOG_LEVEL_{lvl}", lvl == "NONE")

    # App logs: NONE + maximum equals default (no LOG_MAXIMUM_LEVEL_0 in IDF 6)
    for lvl in ("NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"):
        text = b(text, f"CONFIG_LOG_DEFAULT_LEVEL_{lvl}", lvl == "NONE")
    text = b(text, "CONFIG_LOG_MAXIMUM_EQUALS_DEFAULT", True)
    for lvl in ("ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"):
        text = b(text, f"CONFIG_LOG_MAXIMUM_LEVEL_{lvl}", False)

    # Console: UART default (not USB-JTAG) — matches reference power profile
    text = b(text, "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG", False)
    text = b(text, "CONFIG_ESP_CONSOLE_NONE", False)
    text = b(text, "CONFIG_ESP_CONSOLE_UART_DEFAULT", True)
    text = b(text, "CONFIG_ESP_CONSOLE_SECONDARY_NONE", True)

    # Wi-Fi / PHY
    text = b(text, "CONFIG_ESP_WIFI_AMPDU_TX_ENABLED", False)
    text = b(text, "CONFIG_ESP_WIFI_AMPDU_RX_ENABLED", False)
    text = b(text, "CONFIG_ESP_WIFI_NVS_ENABLED", False)
    text = b(text, "CONFIG_ESP_PHY_RF_CAL_PARTIAL", True)
    text = b(text, "CONFIG_ESP_PHY_RF_CAL_NONE", False)
    text = b(text, "CONFIG_ESP_PHY_RF_CAL_FULL", False)

    # Disable ESP-IDF periodic gratuitous ARP (own IP/MAC announce).
    # Official Kconfig: CONFIG_LWIP_ESP_GRATUITOUS_ARP; legacy alias CONFIG_ESP_GRATUITOUS_ARP.
    text = b(text, "CONFIG_LWIP_ESP_GRATUITOUS_ARP", False)
    text = b(text, "CONFIG_ESP_GRATUITOUS_ARP", False)

    # Sleep / clock / PM (skip ESP_SLEEP_PD_DOMAIN_VDDSDIO — not a Kconfig in IDF 6)
    text = b(text, "CONFIG_ESP_SLEEP_POWER_DOWN_FLASH", True)
    text = b(text, "CONFIG_RTC_CLK_SRC_INT_RC", True)
    text = b(text, "CONFIG_RTC_CLK_SRC_EXT_CRYS", False)
    text = b(text, "CONFIG_RTC_CLK_SRC_EXT_OSC", False)
    # Their RTC_CLK_CAL_THRESHOLD=0 → IDF 6: RTC_CLK_CAL_CYCLES=0 (skip cal)
    text = _set_kconfig_value(text, "CONFIG_RTC_CLK_CAL_CYCLES", "0")

    text = b(text, "CONFIG_ESP_BROWNOUT_DET", False)
    text = b(text, "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160", True)
    text = b(text, "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_80", False)
    text = b(text, "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_120", False)

    text = b(text, "CONFIG_ESPTOOLPY_FLASHMODE_DIO", True)
    text = b(text, "CONFIG_ESPTOOLPY_FLASHFREQ_80M", True)
    text = b(text, "CONFIG_ESPTOOLPY_FLASHSIZE_4MB", True)

    text = b(text, "CONFIG_PM_ENABLE", True)
    text = b(text, "CONFIG_FREERTOS_USE_TICKLESS_IDLE", True)

    # Drop unknown/duplicate junk if any prior edits left numeric LOG_DEFAULT_LEVEL
    text = re.sub(
        r"^CONFIG_LOG_DEFAULT_LEVEL=\d+\s*$",
        "CONFIG_LOG_DEFAULT_LEVEL=0",
        text,
        flags=re.M,
    )

    sdk.write_text(text, encoding="utf-8")


def main() -> int:
    host = local_ipv4()
    print(f"UDP server host={host} port={UDP_PORT}", flush=True)
    print("Start receiver: python experiments/udp_rtc_index_server.py", flush=True)

    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    pf.camp.BUILD = BUILD
    pf.BUILD = BUILD

    wifi = pf.camp.APS["chirkov"]
    extra = {"AETHER_DIAG_UDP_RTC_INDEX": "1"}
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
        f"-DWIFI_SSID={wifi['ssid']}",
        f"-DWIFI_PASSWORD={wifi['password']}",
        f"-DSERVICE_UID={pf.camp.SERVICE_UID}",
        f"-DAE_UDP_SERVER_HOST={host}",
        f"-DAE_UDP_SERVER_PORT={UDP_PORT}",
        f"-DPython3_EXECUTABLE={pf.camp.PY.as_posix()}",
        f"-DCMAKE_MAKE_PROGRAM={pf.camp.NINJA.as_posix()}",
        f"-DCMAKE_C_COMPILER={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-gcc.exe').as_posix()}",
        f"-DCMAKE_CXX_COMPILER={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-g++.exe').as_posix()}",
        f"-DCMAKE_OBJCOPY={(pf.camp.RISCV_BIN / 'riscv32-esp-elf-objcopy.exe').as_posix()}",
    ]
    args.extend(pf.clear_power_exp_flags(extra))

    print("cmake configure", flush=True)
    pf.camp.clean_ninja_logs()
    r = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    (BUILD / "cmake.log").write_text(
        (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
    )
    if r.returncode != 0:
        print(r.stderr[-4000:] if r.stderr else r.stdout[-4000:])
        raise SystemExit(f"cmake failed rc={r.returncode}")

    sdk = BUILD / "sdkconfig"
    force_sdk(sdk)
    r2 = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    (BUILD / "cmake2.log").write_text(
        (r2.stdout or "") + "\n" + (r2.stderr or ""), encoding="utf-8"
    )
    if r2.returncode != 0:
        print(r2.stderr[-4000:] if r2.stderr else r2.stdout[-4000:])
        raise SystemExit(f"cmake2 failed rc={r2.returncode}")
    force_sdk(sdk)
    # Reconfigure once more so sdkconfig.h matches final force_sdk.
    r3 = subprocess.run(args, cwd=ROOT, env=pf.camp.env(), capture_output=True, text=True)
    (BUILD / "cmake3.log").write_text(
        (r3.stdout or "") + "\n" + (r3.stderr or ""), encoding="utf-8"
    )
    if r3.returncode != 0:
        print(r3.stderr[-4000:] if r3.stderr else r3.stdout[-4000:])
        raise SystemExit(f"cmake3 failed rc={r3.returncode}")
    force_sdk(sdk)

    sdk_text = sdk.read_text(encoding="utf-8", errors="replace")
    h_text = (BUILD / "config" / "sdkconfig.h").read_text(encoding="utf-8", errors="replace")
    garp_sdk_off = "# CONFIG_LWIP_ESP_GRATUITOUS_ARP is not set" in sdk_text
    garp_h_disabled = "#define CONFIG_LWIP_ESP_GRATUITOUS_ARP 1" not in h_text
    print(
        f"GARP check: sdk_not_set={garp_sdk_off} h_no_define_1={garp_h_disabled}",
        flush=True,
    )
    if not garp_sdk_off or not garp_h_disabled:
        raise SystemExit(
            "CONFIG_LWIP_ESP_GRATUITOUS_ARP still enabled after force_sdk/cmake"
        )

    print("ninja", flush=True)
    e = pf.camp.env()
    e["PATH"] = str(pf.camp.NINJA.parent) + ";" + e.get("PATH", "")
    n = subprocess.run([str(pf.camp.NINJA), "-C", str(BUILD)], cwd=ROOT, env=e)
    if n.returncode != 0:
        raise SystemExit(f"ninja failed rc={n.returncode}")

    print(pf.camp.flash(erase=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
