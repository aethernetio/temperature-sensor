#!/usr/bin/env python3
"""Thermometer 2 cold Wi-Fi connect stage diagnostic — 100× / 2 s sleep.

No UDP. No PPK measurement. Vanilla STA+DHCP, one connect() per cycle.
After 100 cycles firmware dumps CSV over USB Serial/JTAG.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import run_thermometer2_sleep_bisect as sb  # noqa: E402

BUILD = ROOT / "build-wifi-connect-stage-100x"
RESULTS = HERE / "power_factor_results"
CSV_OUT = RESULTS / "wifi_connect_stage_100x.csv"
JSON_OUT = RESULTS / "wifi_connect_stage_100x.json"
MD_OUT = HERE / "WIFI_CONNECT_STAGE_100X.md"

MEASURED = 100
OBSERVE_S = 20.0
SLEEP_S = 2.0

# ESP-IDF wifi_err_reason_t — only well-known codes (do not invent).
REASON_NAMES = {
    1: "UNSPECIFIED",
    2: "AUTH_EXPIRE",
    3: "AUTH_LEAVE",
    4: "ASSOC_EXPIRE",
    5: "ASSOC_TOOMANY",
    6: "NOT_AUTHED",
    7: "NOT_ASSOCED",
    8: "ASSOC_LEAVE",
    9: "ASSOC_NOT_AUTHED",
    15: "4WAY_HANDSHAKE_TIMEOUT",
    200: "BEACON_TIMEOUT",
    201: "NO_AP_FOUND",
    202: "AUTH_FAIL",
    203: "ASSOC_FAIL",
    204: "HANDSHAKE_TIMEOUT",
    205: "CONNECTION_FAIL",
    210: "NO_AP_FOUND_W_COMPATIBLE_SECURITY",
    211: "NO_AP_FOUND_IN_AUTHMODE_THRESHOLD",
    212: "NO_AP_FOUND_IN_RSSI_THRESHOLD",
}


def wifi_creds() -> tuple[str, str]:
    return "chirkov", "kcdjepWz51"


def reason_label(code: int) -> str:
    name = REASON_NAMES.get(int(code))
    if name:
        return f"{code}:{name}"
    return str(int(code))


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    import numpy as np

    return float(np.percentile(np.asarray(vals, dtype=float), p))


def timing_stats(vals: list[float]) -> dict:
    if not vals:
        return {
            "n": 0,
            "median_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "min_ms": None,
            "max_ms": None,
        }
    return {
        "n": len(vals),
        "median_ms": float(statistics.median(vals)),
        "p90_ms": pct(vals, 90),
        "p95_ms": pct(vals, 95),
        "min_ms": float(min(vals)),
        "max_ms": float(max(vals)),
    }


def build_firmware() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    ssid, password = wifi_creds()
    b = BUILD.as_posix()
    defs = (
        f"-D AETHER_DIAG_WIFI_CONNECT_STAGE_100X=1 "
        f"-D BOARD=0 "
        f"-D WIFI_SSID={ssid} "
        f"-D WIFI_PASSWORD={password} "
        f"-B '{b}'"
    )
    extra = f"""
if (-not (Test-Path '{b}/CMakeCache.txt')) {{
  idf.py {defs} set-target esp32c6
  if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
}}
idf.py {defs} build
"""
    r = sb.run(sb.idf_cmd(extra), cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"build failed code={r.returncode}")


def read_dump_from_serial(port: str, timeout_s: float) -> list[str]:
    import serial
    from serial import SerialException

    lines: list[str] = []
    in_dump = False
    t0 = time.time()
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except SerialException as e:
        print(f"serial_open_fail={e}", flush=True)
        return []
    try:
        buf = ""
        while time.time() - t0 < timeout_s:
            try:
                chunk = ser.read(4096)
            except SerialException as e:
                print(f"serial_read_fail={e}", flush=True)
                break
            if chunk:
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip("\r")
                    if line == "BEGIN_WIFI_CONNECT_STAGE_DUMP":
                        in_dump = True
                        lines = []
                        print("DUMP_BEGIN", flush=True)
                        continue
                    if line == "END_WIFI_CONNECT_STAGE_DUMP":
                        print("DUMP_END", flush=True)
                        return lines
                    if in_dump and line and not line.startswith("cycle,"):
                        lines.append(line)
                        if len(lines) % 10 == 0:
                            print(f"dump_rows={len(lines)}", flush=True)
            else:
                time.sleep(0.05)
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return lines


def parse_dump_lines(lines: list[str]) -> list[dict]:
    rows: list[dict] = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 12:
            continue
        try:
            cycle = int(parts[0])
        except ValueError:
            continue
        def i(x: str) -> int:
            return int(x)

        def opt_ms(x: str) -> int | None:
            v = int(x)
            return None if v < 0 else v

        rows.append(
            {
                "cycle": cycle,
                "wifi_start": i(parts[1]),
                "sta_connected": i(parts[2]),
                "got_ip": i(parts[3]),
                "disconnected": i(parts[4]),
                "disconnect_count": i(parts[5]),
                "first_disconnect_reason": i(parts[6]),
                "final_disconnect_reason": i(parts[7]),
                "association_time_ms": opt_ms(parts[8]),
                "dhcp_time_ms": opt_ms(parts[9]),
                "total_connect_time_ms": opt_ms(parts[10]),
                "result_class": parts[11],
                "dhcp_status": i(parts[12]) if len(parts) > 12 else 0,
            }
        )
    rows.sort(key=lambda r: r["cycle"])
    return rows


def analyze(rows: list[dict]) -> dict:
    assert len(rows) == MEASURED
    sta_ok = [r for r in rows if r["sta_connected"] == 1]
    got_ok = [r for r in rows if r["got_ip"] == 1]
    classes = Counter(r["result_class"] for r in rows)

    assoc_times = [
        float(r["association_time_ms"])
        for r in sta_ok
        if r["association_time_ms"] is not None
    ]
    dhcp_times = [
        float(r["dhcp_time_ms"])
        for r in got_ok
        if r["dhcp_time_ms"] is not None
    ]

    reason_all = Counter()
    reason_before = Counter()
    reason_after = Counter()
    for r in rows:
        if r["disconnect_count"] <= 0:
            continue
        code = int(r["first_disconnect_reason"])
        reason_all[code] += 1
        if r["sta_connected"] == 1:
            reason_after[code] += 1
        else:
            reason_before[code] += 1

    sta_n = len(sta_ok)
    got_n = len(got_ok)
    if sta_n <= 25 and got_n <= sta_n:
        stage = "association/authentication"
    elif sta_n >= 90 and got_n <= 30:
        stage = "DHCP"
    elif sta_n > got_n + 5 and sta_n < 90:
        stage = "mixed"
    elif got_n >= 90:
        stage = "undetermined"  # mostly success
    else:
        stage = "mixed" if sta_n > got_n else "association/authentication"

    def reason_table(counter: Counter) -> list[dict]:
        total_disc = sum(counter.values()) or 1
        out = []
        for code, n in counter.most_common():
            out.append(
                {
                    "reason": reason_label(code),
                    "code": int(code),
                    "count": n,
                    "pct_of_100": 100.0 * n / MEASURED,
                    "pct_of_disconnects": 100.0 * n / total_disc,
                }
            )
        return out

    dominant = reason_all.most_common(1)
    dominant_s = (
        f"{reason_label(dominant[0][0])}/{dominant[0][1]}"
        if dominant
        else "none/0"
    )

    return {
        "STA_CONNECTED": {
            "count": sta_n,
            "pct": 100.0 * sta_n / MEASURED,
            "fail_before_connected": MEASURED - sta_n,
        },
        "ASSOCIATION_TIME": timing_stats(assoc_times),
        "GOT_IP": {
            "count": got_n,
            "pct_of_100": 100.0 * got_n / MEASURED,
            "pct_of_sta_connected": (100.0 * got_n / sta_n) if sta_n else None,
        },
        "DHCP_TIME": timing_stats(dhcp_times),
        "RESULT_CLASS": {
            "GOT_IP_SUCCESS": classes.get("GOT_IP_SUCCESS", 0),
            "STA_CONNECTED_BUT_NO_GOT_IP": classes.get(
                "STA_CONNECTED_BUT_NO_GOT_IP", 0
            ),
            "CONNECTED_THEN_DISCONNECTED_BEFORE_IP": classes.get(
                "CONNECTED_THEN_DISCONNECTED_BEFORE_IP", 0
            ),
            "DISCONNECTED_BEFORE_STA_CONNECTED": classes.get(
                "DISCONNECTED_BEFORE_STA_CONNECTED", 0
            ),
            "TIMEOUT_NO_TERMINAL_EVENT": classes.get(
                "TIMEOUT_NO_TERMINAL_EVENT", 0
            ),
        },
        "DISCONNECT_REASONS": reason_table(reason_all),
        "DISCONNECT_BEFORE_STA_CONNECTED": reason_table(reason_before),
        "DISCONNECT_AFTER_STA_CONNECTED": reason_table(reason_after),
        "FAILURE_STAGE": stage,
        "dominant_disconnect": dominant_s,
    }


def write_md(summary: dict, meta: dict) -> None:
    lines = [
        "# THERMOMETER2 WIFI CONNECT STAGE 100x",
        "",
        f"- branch: `{meta.get('branch')}`",
        f"- sha: `{meta.get('sha')}`",
        f"- FAILURE_STAGE: **{summary['FAILURE_STAGE']}**",
        "",
        "## STA_CONNECTED",
        f"- {summary['STA_CONNECTED']}",
        "",
        "## ASSOCIATION TIME",
        f"- {summary['ASSOCIATION_TIME']}",
        "",
        "## GOT_IP",
        f"- {summary['GOT_IP']}",
        "",
        "## DHCP TIME",
        f"- {summary['DHCP_TIME']}",
        "",
        "## RESULT CLASS",
        f"- {summary['RESULT_CLASS']}",
        "",
        "## DISCONNECT REASONS (all)",
    ]
    for r in summary["DISCONNECT_REASONS"]:
        lines.append(
            f"- {r['reason']}: count={r['count']} "
            f"pct100={r['pct_of_100']:.1f}% pct_disc={r['pct_of_disconnects']:.1f}%"
        )
    lines += [
        "",
        "## DISCONNECT before STA_CONNECTED",
        f"- {summary['DISCONNECT_BEFORE_STA_CONNECTED']}",
        "",
        "## DISCONNECT after STA_CONNECTED before GOT_IP",
        f"- {summary['DISCONNECT_AFTER_STA_CONNECTED']}",
        "",
        f"- csv: `{CSV_OUT}`",
        "",
    ]
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    branch, sha = sb.git_meta()
    print(f"branch={branch} sha={sha}", flush=True)

    print("=== BUILD ===", flush=True)
    build_firmware()

    # Power via PPK source meter only (no current capture) so flash is autonomous.
    ppk = sb.PpkSession(3000)
    ppk.open()
    try:
        print("=== FLASH ===", flush=True)
        port = sb.try_flash_with_retries(ppk, BUILD, cold_wait_s=45.0)
        time.sleep(1.0)
        port2 = sb.wait_espressif_com(timeout_s=25.0) or port
        try:
            sb.hard_reset(port2)
        except Exception as e:
            print(f"reset_warn={e}", flush=True)

        print("=== WAIT cold-boot 60s + 100 cycles + dump ===", flush=True)
        t_flash = time.time()
        # Earliest dump: cold 60s + 100 * (fast fail ~1s + 2s sleep) ≈ 360s
        t_earliest = t_flash + 60.0 + MEASURED * 3.0
        t_deadline = t_flash + 60.0 + MEASURED * (OBSERVE_S + SLEEP_S) + 180.0
        dump_lines: list[str] = []
        while time.time() < t_deadline:
            if time.time() < t_earliest:
                time.sleep(5.0)
                print(
                    f"waiting_cycles elapsed={time.time()-t_flash:.0f}s",
                    flush=True,
                )
                continue
            port3 = sb.wait_espressif_com(timeout_s=8.0)
            if not port3:
                time.sleep(2.0)
                continue
            print(f"try_read_dump port={port3}", flush=True)
            dump_lines = read_dump_from_serial(port3, timeout_s=45.0)
            if len(dump_lines) >= MEASURED:
                break
            print(f"partial_dump rows={len(dump_lines)}", flush=True)
            time.sleep(2.0)

        if len(dump_lines) < MEASURED:
            port4 = sb.wait_espressif_com(timeout_s=30.0)
            if port4:
                more = read_dump_from_serial(port4, 180.0)
                if len(more) > len(dump_lines):
                    dump_lines = more

        rows = parse_dump_lines(dump_lines)
        if len(rows) != MEASURED:
            raise RuntimeError(f"expected {MEASURED} rows, got {len(rows)}")

        with CSV_OUT.open("w", encoding="utf-8", newline="") as fh:
            fields = [
                "cycle",
                "wifi_start",
                "sta_connected",
                "got_ip",
                "disconnected",
                "disconnect_count",
                "first_disconnect_reason",
                "final_disconnect_reason",
                "association_time_ms",
                "dhcp_time_ms",
                "total_connect_time_ms",
                "result_class",
            ]
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

        summary = analyze(rows)
        meta = {
            "repo": "aethernetio/temperature-sensor",
            "branch": branch,
            "sha": sha,
            "CONFIG": {
                "cycles": MEASURED,
                "deep_sleep_s": SLEEP_S,
                "connect_timeout_s": OBSERVE_S,
                "cache": "NONE",
                "DHCP": True,
                "WIFI_PS": "NONE",
                "reconnect_within_cycle": False,
                "UDP": False,
                "PPK_measurement": False,
            },
            "BUILD": "PASS",
            "FLASH": "PASS",
            "RUN": "PASS",
            "rows": MEASURED,
            "utc": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "cycles": rows,
            "artifacts": {
                "csv": str(CSV_OUT),
                "json": str(JSON_OUT),
                "report": str(MD_OUT),
            },
        }
        JSON_OUT.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        write_md(summary, meta)
        print(json.dumps(summary, indent=2), flush=True)
        print(
            f"ROOT CAUSE STAGE: STA_CONNECTED={summary['STA_CONNECTED']['count']}/100, "
            f"GOT_IP={summary['GOT_IP']['count']}/100; "
            f"dominant disconnect reason={summary['dominant_disconnect']}; "
            f"failure stage={summary['FAILURE_STAGE']}.",
            flush=True,
        )
        return 0
    finally:
        ppk.close()


if __name__ == "__main__":
    raise SystemExit(main())
