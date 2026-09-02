#!/usr/bin/env python3
"""Machine-readable effective-config audit for power-factor confirmation.

Builds isolated cmake configs for A0/B1/B2/B3/B7 (no flash) and diffs the
effective sdkconfig + compile definitions against A0.

Does not reuse the contaminated shared build-esp32c6-pf-fresh directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import run_prepared_power_factor_study as pf  # noqa: E402

OUT = ROOT / "experiments" / "power_factor_results" / "config_audit"
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
AUDIT_KEYS = (
    "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP",
    "CONFIG_BOOTLOADER_SKIP_VALIDATE_ON_POWER_ON",
    "CONFIG_BOOTLOADER_SKIP_VALIDATE_ALWAYS",
    "CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE",
    "CONFIG_PM_ENABLE",
    "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ",
    "CONFIG_RTC_CLK_SRC_EXT_CRYS",
    "CONFIG_ESP_CONSOLE_NONE",
    "CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG",
    "CONFIG_LOG_DEFAULT_LEVEL_NONE",
    "CONFIG_SECURE_BOOT",
    "CONFIG_FLASH_ENCRYPTION_ENABLED",
)


def parse_sdk_bools(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in AUDIT_KEYS:
        if f"{key}=y" in text or re.search(rf"^{re.escape(key)}=\d+", text, re.M):
            m = re.search(rf"^{re.escape(key)}=(.+)$", text, re.M)
            out[key] = m.group(1).strip() if m else "y"
        elif f"# {key} is not set" in text:
            out[key] = "n"
        else:
            out[key] = "?"
    return out


def expected_factors(variant_id: int) -> dict[str, object]:
    return {
        "variant_id": variant_id,
        "variant_name": pf.variant_name(variant_id),
        "skip_validate_deep_sleep": variant_id in pf.SKIP_VALIDATE_VARIANTS,
        "disconnected_pm_enable": variant_id not in pf.DISC_PM_OFF_VARIANTS,
        "note_runtime": {
            0: "baseline A0",
            10: "sdkconfig SKIP_VALIDATE only",
            11: "sdkconfig DISCONNECTED_PM off only (runtime flag previously unwired)",
            12: "runtime WIFI_PS_MIN via connected_ps/phase_ps",
            16: "runtime CPU 80 MHz via esp_pm_configure",
        }.get(variant_id, ""),
    }


def configure_isolated(ap: str, variant_id: int) -> Path:
    build = ROOT / "build-power-confirm" / "audit" / RUN_ID / f"v{variant_id}" / ap
    if build.exists():
        shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    pf.BUILD = build
    pf.camp.BUILD = build
    pf.CONFIGURED_MARK = build / "_configured.txt"
    pf.cmake_configure_power(ap, variant_id)
    # force_sdk runs again after cmake; reconfigure once so confgen matches.
    pf.force_sdk_measured(variant_id)
    subprocess.run(
        [
            str(pf.camp.CMAKE),
            "-S",
            str(ROOT),
            "-B",
            str(build),
            f"-DAE_POWER_BENCH_VARIANT={variant_id}",
        ],
        cwd=ROOT,
        env=pf.camp.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return build


def snapshot(build: Path, variant_id: int, ap: str) -> dict:
    sdk = build / "sdkconfig"
    text = pf.read_text(sdk)
    parsed = parse_sdk_bools(text)
    out_dir = OUT / RUN_ID / f"v{variant_id}_{ap}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "effective_sdkconfig.txt").write_text(text, encoding="utf-8")
    sdk_h = build / "config" / "sdkconfig.h"
    if sdk_h.exists():
        (out_dir / "sdkconfig.h").write_text(sdk_h.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    defs: list[str] = []
    for p in (
        build / "compile_commands.json",
        build / "esp-idf" / "main" / "CMakeFiles" / "compile_commands.json",
    ):
        if not p.exists():
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"-D(AE_POWER_BENCH_VARIANT=\d+)", raw):
            defs.append(m.group(1))
        for m in re.finditer(r"-D(CONFIG_[A-Z0-9_]+)", raw):
            if "SKIP_VALIDATE" in m.group(1) or "DISCONNECTED_PM" in m.group(1):
                defs.append(m.group(1))
        break
    (out_dir / "compile_definitions.txt").write_text("\n".join(sorted(set(defs))) + "\n", encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    snap = {
        "ap": ap,
        "variant_id": variant_id,
        "expected": expected_factors(variant_id),
        "effective": parsed,
        "sdkconfig_sha256": digest,
        "build_dir": str(build),
        "artifact_dir": str(out_dir),
    }
    (out_dir / "snapshot.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
    return snap


def diff_maps(a: dict[str, str], b: dict[str, str]) -> dict[str, tuple[str, str]]:
    keys = sorted(set(a) | set(b))
    return {k: (a.get(k, "?"), b.get(k, "?")) for k in keys if a.get(k) != b.get(k)}


def main() -> int:
    variants = [0, 10, 11, 12, 16]
    ap = "chirkov"
    OUT.mkdir(parents=True, exist_ok=True)
    snaps: dict[int, dict] = {}
    for vid in variants:
        print(f"configure audit v{vid}...", flush=True)
        build = configure_isolated(ap, vid)
        snaps[vid] = snapshot(build, vid, ap)

    a0 = snaps[0]["effective"]
    diffs = {vid: diff_maps(a0, snaps[vid]["effective"]) for vid in variants if vid != 0}
    expected_only = {
        10: {"CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP"},
        11: {"CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE"},
        12: set(),  # runtime-only
        16: set(),  # runtime-only
    }
    report = {
        "run_id": RUN_ID,
        "BUILD_CONTAMINATION_PRIOR_CAMPAIGN": True,
        "prior_cause": (
            "Shared build-esp32c6-pf-fresh; force_sdk_measured appended "
            "SKIP_VALIDATE for variant 10 and never cleared it. Also "
            "disconnected_pm runtime option was never applied to wifi/sdkconfig."
        ),
        "snapshots": {str(k): v for k, v in snaps.items()},
        "diffs_vs_a0": {str(k): v for k, v in diffs.items()},
        "isolation_ok": True,
        "factor_checks": {},
    }
    for vid, want_keys in expected_only.items():
        got = set(diffs.get(vid, {}))
        # Allow only expected keys to differ (plus unknown noise fails).
        unexpected = got - want_keys
        missing = want_keys - got
        # For runtime-only variants, sdkconfig must match A0 exactly.
        ok = not unexpected and not missing
        if vid == 10:
            ok = ok and snaps[vid]["effective"].get(
                "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP"
            ) == "y" and snaps[0]["effective"].get(
                "CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP"
            ) == "n"
        if vid == 11:
            ok = ok and snaps[vid]["effective"].get(
                "CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE"
            ) == "n" and snaps[0]["effective"].get(
                "CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE"
            ) == "y"
        report["factor_checks"][str(vid)] = {
            "ok": ok,
            "diff_keys": sorted(got),
            "unexpected": sorted(unexpected),
            "missing": sorted(missing),
        }
        if not ok:
            report["isolation_ok"] = False

    out_json = OUT / RUN_ID / "audit_report.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = OUT / RUN_ID / "audit_report.md"
    lines = [
        "# Power-factor config audit",
        "",
        f"run_id: `{RUN_ID}`",
        f"isolation_ok: **{report['isolation_ok']}**",
        "",
        "## Prior campaign contamination",
        "",
        report["prior_cause"],
        "",
        "## Effective key table",
        "",
        "| key | A0 | B1(10) | B2(11) | B3(12) | B7(16) |",
        "|---|---|---|---|---|---|",
    ]
    for key in AUDIT_KEYS:
        row = [key] + [snaps[v]["effective"].get(key, "?") for v in variants]
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## Diffs vs A0", ""]
    for vid, d in diffs.items():
        lines.append(f"### A0 vs {vid} ({pf.variant_name(vid)})")
        if not d:
            lines.append("- (no sdkconfig audit-key differences)")
        else:
            for k, (a, b) in d.items():
                lines.append(f"- `{k}`: `{a}` → `{b}`")
        lines.append("")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"isolation_ok": report["isolation_ok"], "report": str(out_json)}, indent=2))
    return 0 if report["isolation_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
