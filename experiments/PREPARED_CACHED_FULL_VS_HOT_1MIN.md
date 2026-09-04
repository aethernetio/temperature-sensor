# Cached FULL vs HOT — chirkov 1-minute

Run id: `20260903_204622`
Starting SHA: `3db855f56869267190a387ad23b1b0e7980fd6ca`

Warmup FULL (IP/channel/ARP + prepared block) is **not** in the energy window.
Window: start of 2nd (cached) FULL send → end of HOT #10.
Cadence: ≈60 s start-to-start. Best config P4 / CPU80 / SKIP_VALIDATE /
FULL teardown / WIFI_PS=NONE / DISC_PM=ON / encode-during-association / PRE=25 ms.

| send | attempts | RX | loss | energy J | duration s |
|---|---:|---:|---:|---:|---:|
| FULL (cached) | 1 | 1 | 0.0% | 0.784454 | 4.779 |
| HOT | 10 | 9 | 10.0% | 0.238993 avg (0.163560–0.507892) | — |

WINDOW_TOTAL_J=3.201892168
FULL_SEND_J=0.784454210
WINDOW_MINUS_FULL_J=2.417437958
HOT_AVG_J=0.238992629
SLEEP_J_PER_MIN=0.002826057
SLEEP_uA_MEASURED=15.700
ASSUMED_SLEEP_8uA_J_PER_MIN=0.001440000

Decomposition check: WINDOW_MINUS_FULL ≈ 10×HOT_AVG + sleep in the gaps.
HOT_PLUS_SLEEP_PER_MIN_J=0.241818686
HOT_PLUS_SLEEP_AVG_mA=1.343437
CR2_HOT_1MSG_PER_MIN=24.8 d / 0.82 mo

Per-send bursts (after skipping flash+warmup):
  FULL duration=4.779s energy=0.784454 J
Per HOT burst (J): 0.505364, 0.507892, 0.173974, 0.179510, 0.170885, 0.163784, 0.180322, 0.176778, 0.167858, 0.163560

measured_bursts=11 (want 11 = 1 FULL + 10 HOT)
PPK elapsed_s=730.13 energy_J=11.134347341 (includes uncounted flash+warmup)

Notes:
- Warmup FULL + flash (~7.93 J, 57 s) excluded from the window.
- BENCH_ARM was not logged (receiver gap); warmup skipped by first long burst.
- HOT seq 9 missing (9/10). Attempts were still 10; no retry.
- First two HOT bursts ~0.51 J / 2–3 s, then ~0.17 J / ~1 s.
- Sleep ~15.7 µA on PPK (floor/offset); 8 µA assumption is 1.44 mJ/min vs measured 2.83 mJ/min. Negligible vs send energy.
