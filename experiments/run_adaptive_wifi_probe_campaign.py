#!/usr/bin/env python3
"""Adaptive Wi-Fi probe campaign: Phase A → B → C on chirkov then aethernetio.

Server protocol unchanged. No server deploy.
COM disappear during 1s deep sleep is expected.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\nickc\Projects\temperature-sensor-prepared")
BUILD = ROOT / "build-esp32c6-adaptive-probe"
AETHER = r"C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0"
PY = Path(r"C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe")
CMAKE = Path(r"C:\Espressif\tools\cmake\3.30.2\bin\cmake.exe")
NINJA = Path(r"C:\Espressif\tools\ninja\1.12.1\ninja.exe")
RX_EXE = ROOT / "temperature_receiver" / "build-bisect" / "temperature_receiver.exe"
RX_BUILD = ROOT / "temperature_receiver" / "build-bisect"
IDF_PATH = r"C:\Espressif\frameworks\esp-idf-v6.0.2"
TOOLCHAIN = Path(IDF_PATH) / "tools" / "cmake" / "toolchain-esp32c6.cmake"
CCACHE = r"C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64"
USER_CONFIG = "main/user_config_full_quiet.h"
FS_INIT = ROOT / "experiments" / "preprovision" / "sender_fs_157aadbe.h"
PREPARED_RX_SESSION = ROOT / "experiments" / "prepared_wifi_cache_rx_session"
# Must match persisted desktop receiver session (see prepared_* rx logs).
SERVICE_UID = "5aade50f-00d9-4624-b097-e203cdcf1e38"
OUT = ROOT / "experiments" / "adaptive_probe_results"
PROGRESS = ROOT / "experiments" / "adaptive_probe_progress.log"
PORT = "COM7"

APS = {
    "chirkov": {
        "ssid": "chirkov",
        "password": "kcdjepWz51",
    },
    "aethernetio": {
        "ssid": "aethernetio",
        "password": "12481632",
    },
}


def env() -> dict:
    e = os.environ.copy()
    e["IDF_PATH"] = IDF_PATH
    e["IDF_TOOLS_PATH"] = r"C:\Espressif"
    extra = [
        CCACHE,
        r"C:\Espressif\tools\ninja\1.12.1",
        r"C:\Espressif\tools\cmake\3.30.2\bin",
        r"C:\Espressif\tools\riscv32-esp-elf\esp-15.2.0_20251204\riscv32-esp-elf\bin",
        r"C:\msys64\ucrt64\bin",
        r"C:\Program Files\Git\cmd",
    ]
    e["Path"] = ";".join(extra) + ";" + e.get("Path", "")
    e.pop("CCACHE_DISABLE", None)
    return e


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_port(timeout_s: float = 120.0) -> str | None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        r = subprocess.run(
            [
                str(PY),
                "-c",
                "import serial.tools.list_ports as lp\n"
                "ports=[p.device for p in lp.comports() if p.vid==0x303A]\n"
                "print(ports[0] if ports else '')\n",
            ],
            capture_output=True,
            text=True,
            env=env(),
        )
        p = (r.stdout or "").strip()
        if p:
            return p
        # fallback COM7 presence
        r2 = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-PnpDevice -Class Ports -Status OK | Where-Object {{ $_.FriendlyName -match '{PORT}' }}).Count",
            ],
            capture_output=True,
            text=True,
        )
        if (r2.stdout or "").strip() not in ("", "0"):
            return PORT
        time.sleep(1.5)
    return None


def force_sdk_fixes() -> None:
    sdk = BUILD / "sdkconfig"
    if not sdk.exists():
        return
    text = sdk.read_text(encoding="utf-8")
    reps = [
        ("CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=y", "# CONFIG_ESP_WIFI_ENABLE_WPA3_SAE is not set"),
        ("CONFIG_RTC_CLK_SRC_INT_RC=y", "# CONFIG_RTC_CLK_SRC_INT_RC is not set"),
        ("# CONFIG_RTC_CLK_SRC_EXT_CRYS is not set", "CONFIG_RTC_CLK_SRC_EXT_CRYS=y"),
        ("CONFIG_ESP_BROWNOUT_DET=n", "CONFIG_ESP_BROWNOUT_DET=y"),
        ("# CONFIG_ESP_BROWNOUT_DET is not set", "CONFIG_ESP_BROWNOUT_DET=y"),
        ("CONFIG_PM_ENABLE=y", "# CONFIG_PM_ENABLE is not set"),
        ("CONFIG_RTC_CLK_CAL_CYCLES=0", "CONFIG_RTC_CLK_CAL_CYCLES=1024"),
        ("CONFIG_ESP_CONSOLE_UART_DEFAULT=y", "# CONFIG_ESP_CONSOLE_UART_DEFAULT is not set"),
        ("# CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG is not set", "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y"),
        ("CONFIG_ESP_CONSOLE_NONE=y", "# CONFIG_ESP_CONSOLE_NONE is not set"),
        ("CONFIG_LOG_DEFAULT_LEVEL_NONE=y", "# CONFIG_LOG_DEFAULT_LEVEL_NONE is not set"),
        ("# CONFIG_LOG_DEFAULT_LEVEL_INFO is not set", "CONFIG_LOG_DEFAULT_LEVEL_INFO=y"),
        ("CONFIG_LOG_DEFAULT_LEVEL=0", "CONFIG_LOG_DEFAULT_LEVEL=3"),
    ]
    for a, b in reps:
        text = text.replace(a, b)
    if "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y" not in text:
        text += "\nCONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y\n"
    if "CONFIG_RTC_CLK_SRC_EXT_CRYS=y" not in text:
        text += "\nCONFIG_RTC_CLK_SRC_EXT_CRYS=y\n"
    sdk.write_text(text, encoding="utf-8")


def seed_usb_console_sdkconfig() -> None:
    """Prefer a known-good ESP32-C6 sdkconfig with USB Serial/JTAG console."""
    donor = ROOT / "build-esp32c6-full-aether-baseline" / "sdkconfig"
    BUILD.mkdir(parents=True, exist_ok=True)
    dst = BUILD / "sdkconfig"
    if donor.exists() and not dst.exists():
        shutil.copy2(donor, dst)
        log("seeded sdkconfig from full-aether-baseline (USB console)")
    force_sdk_fixes()


def clear_exp_flags(extra: dict[str, str]) -> list[str]:
    flags = [
        "AE_EXP_PREPARED_WIFI_CACHE_5X20",
        "AE_EXP_PREPARED_KEEP_WIFI_UP_5X20",
        "AE_EXP_PREPARED_WIFI_FASTEST",
        "AE_EXP_PREPARED_DEEPSLEEP_5X50",
        "AE_EXP_PREPARED_FINAL_D1_5X50",
        "AE_EXP_PREPARED_AP_AETHERNETIO_3X10",
        "AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5",
        "AE_EXP_FULL_AETHER_AETHERNETIO",
        "AE_EXP_ADAPTIVE_WIFI_PROBE_A",
        "AE_EXP_ADAPTIVE_WIFI_PROBE_B",
        "AE_EXP_ADAPTIVE_WIFI_PROBE_C",
        "AE_EXP_PREPARED_TX_DONE_DIAG",
        "AE_EXP_PREPARED_MAC_RETRY_DIAG",
        "AE_EXP_PREPARED_BOOT_WIFI_OPT",
        "AE_EXP_PREPARED_BOOT_WIFI_VAL100",
        "AE_EXP_PREPARED_WIFI_BISECT",
        "AE_EXP_PREPARED_MESSAGE_E2E",
        "AE_EXP_WIFI_LIFECYCLE",
        "AE_EXP_FULL_CYCLES",
    ]
    out = []
    for f in flags:
        out.append(f"-D{f}={extra.get(f, '')}")
    return out


def cmake_configure(ap: str, phase: str, defs: dict[str, str]) -> None:
    wifi = APS[ap]
    if BUILD.exists() and not (BUILD / "CMakeCache.txt").exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    cache = BUILD / "CMakeCache.txt"
    if cache.exists():
        text = cache.read_text(encoding="utf-8", errors="replace")
        if "IDF_TARGET:STRING=esp32c6" not in text:
            log("wiping non-ESP-IDF build dir")
            shutil.rmtree(BUILD, ignore_errors=True)
        elif "CMAKE_CXX_COMPILER:FILEPATH=" in text and "msys64" in text:
            log("wiping host-msys build dir")
            shutil.rmtree(BUILD, ignore_errors=True)
    seed_usb_console_sdkconfig()
    args = [
        str(CMAKE),
        "-S",
        str(ROOT),
        "-B",
        str(BUILD),
        "-G",
        "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={TOOLCHAIN.as_posix()}",
        "-DIDF_TARGET=esp32c6",
        f"-DCPM_aether-client-cpp_SOURCE={AETHER}",
        f"-DUSER_CONFIG={USER_CONFIG}",
        f"-DFS_INIT={FS_INIT.as_posix()}",
        f"-DSDKCONFIG={BUILD.as_posix()}/sdkconfig",
        "-DAE_DISTILLATION=ON",
        "-DAE_FILTRATION=ON",
        "-DAE_EXP_SKIP_DTOR_SAVE=1",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DWIFI_SSID={wifi['ssid']}",
        f"-DWIFI_PASSWORD={wifi['password']}",
        f"-DSERVICE_UID={SERVICE_UID}",
    ]
    phase_key = {
        "A": "AE_EXP_ADAPTIVE_WIFI_PROBE_A",
        "B": "AE_EXP_ADAPTIVE_WIFI_PROBE_B",
        "C": "AE_EXP_ADAPTIVE_WIFI_PROBE_C",
    }[phase]
    extra = {phase_key: "1"}
    args.extend(clear_exp_flags(extra))
    for k, v in defs.items():
        args.append(f"-D{k}={v}")
    log(f"cmake phase={phase} ap={ap}")
    r = subprocess.run(args, cwd=ROOT, env=env(), capture_output=True, text=True)
    if r.returncode != 0:
        (OUT / f"cmake_{ap}_{phase}.err").write_text(
            (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
        )
        raise RuntimeError(f"cmake failed {ap} {phase}")
    force_sdk_fixes()


def ninja_build() -> None:
    log("ninja build")
    r = subprocess.run(
        [str(NINJA), "-C", str(BUILD)], env=env(), capture_output=True, text=True
    )
    if r.returncode != 0:
        (OUT / "ninja.err").write_text(
            (r.stdout or "")[-30000:] + "\n" + (r.stderr or "")[-10000:],
            encoding="utf-8",
        )
        raise RuntimeError("ninja failed")
    log("build ok")


def flash(erase: bool = False) -> str:
    port = find_port(180)
    if not port:
        raise RuntimeError("no ESP COM for flash")
    if erase:
        log(f"erase-flash {port}")
        subprocess.run(
            [
                str(PY),
                "-m",
                "esptool",
                "--chip",
                "esp32c6",
                "-p",
                port,
                "erase-flash",
            ],
            cwd=str(BUILD),
            env=env(),
            check=False,
        )
        time.sleep(2)
        port = find_port(60) or port
    log(f"flash {port}")
    r = subprocess.run(
        [
            str(PY),
            "-m",
            "esptool",
            "--chip",
            "esp32c6",
            "-p",
            port,
            "-b",
            "460800",
            "--before",
            "default-reset",
            "--after",
            "hard-reset",
            "write-flash",
            "@flash_args",
        ],
        cwd=str(BUILD),
        env=env(),
        capture_output=True,
        text=True,
    )
    (OUT / "flash_last.log").write_text(
        (r.stdout or "") + "\n" + (r.stderr or ""), encoding="utf-8"
    )
    if r.returncode != 0:
        raise RuntimeError(f"flash failed rc={r.returncode}")
    return port


def capture_until(port: str, done_token: str, timeout_s: float, out_path: Path) -> str:
    log(f"capture {done_token} timeout={timeout_s}s -> {out_path.name}")
    script = f"""
import serial, time, sys
port={port!r}
done={done_token!r}
timeout={timeout_s}
out={str(out_path)!r}
# wait for port after flash reset
for _ in range(40):
    try:
        ser=serial.Serial(port, 115200, timeout=0.2)
        break
    except Exception:
        time.sleep(0.5)
else:
    open(out,'w',encoding='utf-8').write('CAPTURE_OPEN_FAIL\\n')
    raise SystemExit(2)
t0=time.time()
buf=[]
f=open(out,'w',encoding='utf-8')
try:
    while time.time()-t0 < timeout:
        try:
            line=ser.readline()
        except Exception:
            time.sleep(0.5)
            continue
        if not line:
            continue
        s=line.decode('utf-8','replace')
        sys.stdout.write(s); sys.stdout.flush()
        f.write(s); f.flush()
        buf.append(s)
        if done in s:
            break
finally:
    try:
        ser.close()
    except Exception:
        pass
    f.close()
"""
    r = subprocess.run([str(PY), "-c", script], env=env(), capture_output=True, text=True)
    text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
    # also keep raw stdout/stderr for debugging
    (OUT / (out_path.stem + "_capture_meta.txt")).write_text(
        f"rc={r.returncode}\\nstdout_tail={(r.stdout or '')[-2000:]}\\nstderr_tail={(r.stderr or '')[-2000:]}\\n",
        encoding="utf-8",
    )
    if done_token not in text:
        log(f"WARN missing {done_token}; stderr={(r.stderr or '')[-400:]}")
    return text


def parse_a_winner(text: str) -> dict:
    m = re.search(
        r"A_WINNER profile=(-?\d+) pre_ms=(\d+) loss=([0-9.]+) connect_median_ms=(\d+)",
        text,
    )
    sums = re.findall(
        r"A_SUM profile=(\d+) pre=(\d+) connect_ok=(\d+) ready_ok=(\d+) "
        r"icmp_sent=(\d+) icmp_recv=(\d+) icmp_loss=([0-9.]+) fail=(\d+) "
        r"connect_median_ms=(\d+) connect_p90_ms=(\d+) connect_max_ms=(\d+)",
        text,
    )
    pres = re.findall(
        r"A_PRE profile=(\d+) pre=(\d+) connect_ok=(\d+) .* icmp_recv=(\d+) icmp_loss=([0-9.]+) fail=(\d+)",
        text,
    )
    out = {
        "winner_profile": int(m.group(1)) if m else -1,
        "pre_ms": int(m.group(2)) if m else 300,
        "loss": float(m.group(3)) if m else 100.0,
        "connect_median_ms": int(m.group(4)) if m else 0,
        "baseline": [],
        "pre_search": [],
    }
    for s in sums:
        out["baseline"].append(
            {
                "profile": int(s[0]),
                "pre": int(s[1]),
                "connect_ok": int(s[2]),
                "ready_ok": int(s[3]),
                "icmp_sent": int(s[4]),
                "icmp_recv": int(s[5]),
                "icmp_loss": float(s[6]),
                "fail": int(s[7]),
                "median": int(s[8]),
                "p90": int(s[9]),
                "max": int(s[10]),
            }
        )
    for p in pres:
        out["pre_search"].append(
            {
                "profile": int(p[0]),
                "pre": int(p[1]),
                "connect_ok": int(p[2]),
                "icmp_recv": int(p[3]),
                "icmp_loss": float(p[4]),
                "fail": int(p[5]),
            }
        )
    return out


def parse_b_sum(text: str) -> dict:
    m = re.search(
        r"B_SUM ping_sent=(\d+) ping_ok=(\d+) ping_late=(\d+) ping_error=(\d+) "
        r"ping_timeout=(\d+) cold_median_ms=(\d+) cold_p90_ms=(\d+) "
        r"rtt_median_ms=(\d+) rtt_p90_ms=(\d+)",
        text,
    )
    if not m:
        return {}
    return {
        "ping_sent": int(m.group(1)),
        "ping_ok": int(m.group(2)),
        "ping_late": int(m.group(3)),
        "ping_error": int(m.group(4)),
        "ping_timeout": int(m.group(5)),
        "cold_median_ms": int(m.group(6)),
        "cold_p90_ms": int(m.group(7)),
        "rtt_median_ms": int(m.group(8)),
        "rtt_p90_ms": int(m.group(9)),
    }


def kill_receiver() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "temperature_receiver.exe"],
        capture_output=True,
        text=True,
    )
    time.sleep(1)


def receiver_alive() -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq temperature_receiver.exe"],
        capture_output=True,
        text=True,
    )
    return "temperature_receiver.exe" in (r.stdout or "")


def parse_receiver_uid(rx_log: Path) -> str | None:
    if not rx_log.exists():
        return None
    m = re.search(
        r"RECEIVER_UID=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        rx_log.read_text(encoding="utf-8", errors="replace"),
    )
    return m.group(1).lower() if m else None


def start_receiver(tag: str, session: Path, tsv: Path, rx_log: Path) -> None:
    kill_receiver()
    # Keep preprovisioned receiver identity; wiping session breaks P2P UID match.
    session = PREPARED_RX_SESSION
    session.mkdir(parents=True, exist_ok=True)
    if tsv.exists():
        tsv.unlink()
    env2 = env()
    env2["AE_RECEIVER_SESSION_DIR"] = str(session)
    env2["AE_DS_TSV"] = str(tsv)
    env2["AE_DS_BENCH_TAG"] = tag
    err = rx_log.with_suffix(rx_log.suffix + ".err")
    with rx_log.open("w", encoding="utf-8") as outf, err.open("w", encoding="utf-8") as errf:
        subprocess.Popen(
            [str(RX_EXE)],
            cwd=str(session),
            env=env2,
            stdout=outf,
            stderr=errf,
        )
    t0 = time.time()
    uid = None
    while time.time() - t0 < 90:
        uid = parse_receiver_uid(rx_log)
        if uid:
            break
        time.sleep(1)
    if not uid:
        raise RuntimeError("receiver not ready")
    if uid != SERVICE_UID.lower():
        raise RuntimeError(
            f"receiver UID mismatch: got {uid} expected {SERVICE_UID.lower()}"
        )
    log(f"receiver ready uid={uid}")


def analyze_tsv(tsv: Path) -> dict:
    if not tsv.exists():
        return {"received": 0, "hot": 0, "full": 0}
    lines = [ln for ln in tsv.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        return {"received": 0, "hot": 0, "full": 0}
    start = 1 if lines[0].startswith("record_id") or "\tkind\t" in lines[0] else 0
    hot = 0
    full = 0
    seqs = []
    for ln in lines[start:]:
        parts = ln.split("\t") if "\t" in ln else ln.split(",")
        if len(parts) < 4:
            continue
        try:
            kind = parts[1].strip()
            seq = int(float(parts[15])) if len(parts) > 15 else int(float(parts[3]))
        except Exception:
            continue
        if kind == "2" or "hot" in kind.lower():
            hot += 1
            seqs.append(seq)
        elif kind == "1" or "full" in kind.lower():
            full += 1
    missing = 0
    if seqs:
        smin, smax = min(seqs), max(seqs)
        have = set(seqs)
        missing = sum(1 for s in range(smin, smax + 1) if s not in have)
    return {
        "received": len(lines) - start,
        "hot": hot,
        "full": full,
        "missing_in_span": missing,
        "seq_min": min(seqs) if seqs else 0,
        "seq_max": max(seqs) if seqs else 0,
    }


def run_phase_a(ap: str, results: dict) -> None:
    cmake_configure(ap, "A", {})
    ninja_build()
    port = flash(erase=True)
    time.sleep(2)
    # Phase A can take a long time: 5*30 + PRE searches.
    text = capture_until(port, "A_DONE", 6 * 60 * 60, OUT / f"{ap}_phase_a.log")
    parsed = parse_a_winner(text)
    results[ap]["phase_a"] = parsed
    (OUT / f"{ap}_phase_a.json").write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    log(f"A winner profile={parsed['winner_profile']} pre={parsed['pre_ms']}")


def run_phase_b(ap: str, results: dict) -> None:
    cmake_configure(
        ap,
        "B",
        {
            "AE_PING_CYCLES": "50",
            "AE_RELIABILITY_CLIENT_ID": "reliability_full_v1",
        },
    )
    ninja_build()
    port = flash(erase=False)
    time.sleep(2)
    text = capture_until(port, "B_DONE", 4 * 60 * 60, OUT / f"{ap}_phase_b_canonical.log")
    results[ap]["phase_b_canonical"] = parse_b_sum(text)
    (OUT / f"{ap}_phase_b_canonical.json").write_text(
        json.dumps(results[ap]["phase_b_canonical"], indent=2), encoding="utf-8"
    )

    # Optional preferred_channel-only fast hint if winner uses channel.
    a = results[ap].get("phase_a", {})
    wp = int(a.get("winner_profile", -1))
    ch = 0
    # Channel not in A_WINNER line; leave NOT_TESTED for IP/ARP, try channel=0 means canonical.
    results[ap]["phase_b_selected_profile"] = {
        "status": "NOT_TESTED",
        "reason": "FULL Aether path cannot apply cached IP/ARP without invasive Wi-Fi lifecycle bypass; preferred_channel alone is incomplete vs ICMP winner.",
        "winner_profile": wp,
    }


def run_phase_c(ap: str, results: dict) -> None:
    a = results[ap].get("phase_a", {})
    wp = max(0, int(a.get("winner_profile", 0)))
    pre = max(50, int(a.get("pre_ms", 300)))
    session = OUT / f"{ap}_rx_session"
    tsv = OUT / f"{ap}_phase_c.tsv"
    rx_log = OUT / f"{ap}_phase_c_rx.log"

    # Baseline POST=300, 5x30, sleep 1s
    cmake_configure(
        ap,
        "C",
        {
            "AE_PROBE_OUTER": "5",
            "AE_PROBE_HOT_PER_OUTER": "30",
            "AE_PROBE_PROFILE": str(wp),
            "AE_PROBE_PRE_MS": str(pre),
            "AE_PROBE_POST_MS": "300",
            "AE_PROBE_SLEEP_US": "1000000",
            "AE_PROBE_RUN_ID": "1",
            "BENCH_CLIENT_ID": "reliability_full_v1",
            "AETHER_PREPARED_NONCE_RESERVE": "30",
        },
    )
    ninja_build()
    start_receiver(f"adaptive_c_{ap}", session, tsv, rx_log)
    flash(erase=True)
    # 5 FULL + 150 HOT * ~1s sleep ≈ 200s+; allow 45 min
    log("phase C baseline wait for HOT delivery")
    t0 = time.time()
    target_hot = 150
    while time.time() - t0 < 45 * 60:
        if not receiver_alive():
            log("receiver died — restarting")
            start_receiver(f"adaptive_c_{ap}", session, tsv, rx_log)
        stats = analyze_tsv(tsv)
        log(f"C progress hot={stats.get('hot', 0)}/{target_hot} full={stats.get('full', 0)}")
        if stats.get("hot", 0) >= target_hot:
            break
        # COM may disappear during sleep — do not treat as crash
        time.sleep(10)
    base = analyze_tsv(tsv)
    results[ap]["phase_c_baseline"] = {
        "profile": wp,
        "pre_ms": pre,
        "post_ms": 300,
        "delivery": base,
    }

    # POST search via P2P delivery
    post_winner = 300
    for post in (200, 100, 50, 25, 10, 0):
        cmake_configure(
            ap,
            "C",
            {
                "AE_PROBE_OUTER": "1",
                "AE_PROBE_HOT_PER_OUTER": "30",
                "AE_PROBE_PROFILE": str(wp),
                "AE_PROBE_PRE_MS": str(pre),
                "AE_PROBE_POST_MS": str(post),
                "AE_PROBE_SLEEP_US": "1000000",
                "AE_PROBE_RUN_ID": str(10 + post),
                "BENCH_CLIENT_ID": "reliability_full_v1",
                "AETHER_PREPARED_NONCE_RESERVE": "30",
            },
        )
        ninja_build()
        tsv_p = OUT / f"{ap}_phase_c_post{post}.tsv"
        start_receiver(f"adaptive_c_{ap}_post{post}", session, tsv_p, OUT / f"{ap}_post{post}_rx.log")
        # wait wake window then flash
        find_port(90)
        flash(erase=False)
        t0 = time.time()
        while time.time() - t0 < 20 * 60:
            st = analyze_tsv(tsv_p)
            if st.get("hot", 0) >= 28:
                break
            time.sleep(5)
        st = analyze_tsv(tsv_p)
        results[ap].setdefault("phase_c_post", []).append({"post_ms": post, "delivery": st})
        # Per-run target is 30 HOT; allow 2 losses vs baseline total (150).
        if st.get("hot", 0) >= 28:
            post_winner = post
        else:
            log(f"POST {post} FAIL hot={st.get('hot')}; stop search")
            break
    results[ap]["phase_c_post_winner"] = post_winner

    # Sleep variants 250/500/1000
    for sleep_ms in (250, 500, 1000):
        cmake_configure(
            ap,
            "C",
            {
                "AE_PROBE_OUTER": "1",
                "AE_PROBE_HOT_PER_OUTER": "30",
                "AE_PROBE_PROFILE": str(wp),
                "AE_PROBE_PRE_MS": str(pre),
                "AE_PROBE_POST_MS": str(post_winner),
                "AE_PROBE_SLEEP_US": str(sleep_ms * 1000),
                "AE_PROBE_RUN_ID": str(100 + sleep_ms),
                "BENCH_CLIENT_ID": "reliability_full_v1",
                "AETHER_PREPARED_NONCE_RESERVE": "30",
            },
        )
        ninja_build()
        tsv_s = OUT / f"{ap}_phase_c_sleep{sleep_ms}.tsv"
        start_receiver(f"adaptive_c_{ap}_s{sleep_ms}", session, tsv_s, OUT / f"{ap}_s{sleep_ms}_rx.log")
        find_port(90)
        flash(erase=False)
        t0 = time.time()
        while time.time() - t0 < 20 * 60:
            if analyze_tsv(tsv_s).get("hot", 0) >= 28:
                break
            time.sleep(5)
        results[ap].setdefault("phase_c_sleep", []).append(
            {"sleep_ms": sleep_ms, "delivery": analyze_tsv(tsv_s)}
        )

    # Long run 500 HOT
    cmake_configure(
        ap,
        "C",
        {
            "AE_PROBE_OUTER": "10",
            "AE_PROBE_HOT_PER_OUTER": "50",
            "AE_PROBE_PROFILE": str(wp),
            "AE_PROBE_PRE_MS": str(pre),
            "AE_PROBE_POST_MS": str(post_winner),
            "AE_PROBE_SLEEP_US": "1000000",
            "AE_PROBE_RUN_ID": "500",
            "BENCH_CLIENT_ID": "reliability_full_v1",
            "AETHER_PREPARED_NONCE_RESERVE": "50",
        },
    )
    ninja_build()
    tsv_l = OUT / f"{ap}_phase_c_long.tsv"
    start_receiver(f"adaptive_c_{ap}_long", session, tsv_l, OUT / f"{ap}_long_rx.log")
    find_port(90)
    flash(erase=False)
    t0 = time.time()
    while time.time() - t0 < 3 * 60 * 60:
        st = analyze_tsv(tsv_l)
        log(f"long hot={st.get('hot', 0)}/500")
        if st.get("hot", 0) >= 500:
            break
        time.sleep(15)
    results[ap]["phase_c_long"] = analyze_tsv(tsv_l)
    kill_receiver()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if PROGRESS.exists():
        PROGRESS.unlink()
    results = {
        "SERVER_CHANGED": "no",
        "SERVER_PROTOCOL": "current_existing_only",
        "chirkov": {},
        "aethernetio": {},
    }
    aps = sys.argv[1:] or ["chirkov", "aethernetio"]
    for ap in aps:
        log(f"==== AP {ap} BEGIN ====")
        # invalidate cache by erase on Phase A
        run_phase_a(ap, results)
        run_phase_b(ap, results)
        run_phase_c(ap, results)
        (OUT / f"{ap}_all.json").write_text(json.dumps(results[ap], indent=2), encoding="utf-8")
        log(f"==== AP {ap} DONE ====")
    (OUT / "campaign.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    log("campaign complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"FATAL: {e}")
        raise
