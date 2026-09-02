# Prepared Power Factor Confirmation

run_id: `20260902_112517`  
Hardware: ESP32-C6, PPK 3000 mV, 100 HOT, deep sleep 2000 ms  
Profiles: chirkov P4/PRE25/POST0; aethernetio P4/PRE0/POST0

## Verdict

**BUILD_CONTAMINATION_FOUND=yes** in the prior 35-variant campaign.

Shared `build-esp32c6-pf-fresh` plus append-only
`CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP` for B1 caused later variants
(B2/B3/B7/…) to inherit skip-validate. That is why those wakes clustered at
~353–354 ms. Independently, `PowerBenchOptions.disconnected_pm` was never
applied at runtime; B2 must toggle `CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE`.

Isolated confirmation (`build-power-confirm/<run>/<cfm>/<ap>/`) with set/clear
Kconfig + sdkconfig.h lock shows:

| Factor | Confirmed? | Notes |
|---|---|---|
| B1 SKIP_VALIDATE | **YES** (~25% mean) | Strong production candidate |
| B2 DISC_PM_OFF | **NO** | ≈A0 when correctly applied |
| B3 WIFI_PS_MIN | **NO** | ≈A0 wake/energy |
| B7 CPU80 | weak (~8% mean, ~2% median) | Stacks with SKIP |
| IO_TEARDOWN (=DirectDeepSleep) | **AP-dependent** | Harms chirkov; helps aethernetio |

**Best combination:** `SKIP_VALIDATE + CPU80` (full teardown, not DirectDeepSleep).

## Main table

| variant | AP | exact changed factors | RX/100 | E_mean mJ | E_median mJ | E_p90 mJ | wake_mean ms | wake_median ms | wake_p90 ms | delta mean % | delta median % | PASS |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| CFM00_A0_CHIRKOV_BEFORE | chirkov | (baseline A0) | 99 | 176.0 | 152.4 | 182.4 | — | 660 | — | — | — | YES |
| CFM01_B1_SKIP_VALIDATE | chirkov | BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP | 94 | 123.0 | 105.5 | 130.0 | — | 361 | — | −24.6 | −29.7 | YES |
| CFM02R_B2_DISC_PM_OFF | chirkov | STA_DISCONNECTED_PM=OFF | 98 | 248.1 | 155.4 | 190.7 | — | 665 | — | +52.2 | +3.5 | YES |
| CFM03_B3_WIFI_PS_MIN | chirkov | WIFI_PS_MIN | 100 | 229.0 | 149.4 | 175.5 | — | 639 | — | +40.5 | −0.4 | YES |
| CFM04_B7_CPU80 | chirkov | CPU80 | 99 | 149.5 | 147.3 | 158.3 | — | 655 | — | −8.3 | −1.9 | YES |
| CFM05_A0_CHIRKOV_AFTER | chirkov | (baseline A0) | 100 | 150.1 | 147.8 | 162.5 | — | 645 | — | — | — | YES |
| CFM06_IO_TEARDOWN_CHIRKOV | chirkov | DirectDeepSleep (skip wifi stop/deinit) | 96 | 501.6 | 504.1 | 518.1 | — | 1764 | — | +207.7 | +235.8 | YES* |
| CFM10R_A0_AETHERNETIO_BEFORE | aethernetio | (baseline A0) | 100 | 168.2 | 153.5 | 178.1 | — | 665 | — | — | — | YES |
| CFM11_IO_TEARDOWN_AETHERNETIO | aethernetio | DirectDeepSleep | 100 | 115.4 | 114.0 | 123.4 | — | 563 | — | −29.6 | −25.7 | YES |
| CFM12_A0_AETHERNETIO_AFTER | aethernetio | (baseline A0) | 98 | 159.6 | 153.3 | 167.6 | — | 661 | — | — | — | YES |
| CFM40_SKIP_CPU80 | chirkov | SKIP_VALIDATE+CPU80 | 98 | 113.9 | 102.9 | 123.5 | — | 373 | — | −30.1 | −31.4 | YES |
| CFM40_SKIP_CPU80_REPEAT | chirkov | SKIP_VALIDATE+CPU80 | 91 | 118.7 | 101.0 | 124.7 | — | 363 | — | −27.2 | −32.7 | YES |
| CFM40_SKIP_CPU80_AETHERNETIO | aethernetio | SKIP_VALIDATE+CPU80 | 100 | 130.8 | 126.1 | 153.9 | — | 460 | — | −20.2 | −17.8 | YES |

\*PASS on RX≥90, but energy is pathological vs baseline (not a candidate).

Deltas use average of before/after own-AP baseline (mean/median separately).

### Baseline drift

- chirkov A0 median drift before→after: **3.0%** (≤5% → OK)
- chirkov A0 mean drift: 14.7% (before had heavy right-tail; after mean≈median)
- aethernetio A0 median drift (CFM10R→CFM12): **0.14%**

### Build isolation audit

Prior campaign contamination: append-only SKIP_VALIDATE in shared build dir.

Confirmation: each CFM uses a wiped `build-power-confirm/<run>/<cfm>/<ap>/`,
reseeds donor sdkconfig, then **set/clear** sticky symbols and mirrors them into
`config/sdkconfig.h`. Artifacts per CFM: `effective_sdkconfig.txt`,
`compile_definitions.txt`, `firmware.sha256`, `variant.json`.

Effective diffs vs A0 (confirmation):

| key | A0 | B1 | B2 | B3 | B7 |
|---|---|---|---|---|---|
| SKIP_VALIDATE_IN_DEEP_SLEEP | n | **y** | n | n | n |
| SKIP_VALIDATE_ON_POWER_ON | n | n | n | n | n |
| SKIP_VALIDATE_ALWAYS | n | n | n | n | n |
| STA_DISCONNECTED_PM | y | y | **n** | y | n/a→y |
| CPU (runtime) | 160 | 160 | 160 | 160 | **80** |
| WIFI_PS (runtime) | NONE | NONE | NONE | **MIN** | NONE |
| teardown | full | full | full | full | full |

B3/B7 sdkconfig matches A0 (runtime-only factors). B1/B2 change only their
intended Kconfig bits after the lock fix.

### IO_TEARDOWN semantics

Variant **206** sets `TeardownPolicy::kDirectDeepSleep` — **same policy as B12**.

After successful TX-done, `CleanupHotPathWifiRuntime(2)`:

1. returns immediately
2. does **not** unregister Wi-Fi/IP handlers
3. does **not** call `esp_wifi_stop`
4. does **not** call `esp_wifi_deinit`
5. does **not** destroy netif / event group

Deep sleep is armed by the bench afterward. Name “IO_TEARDOWN” is misleading.

| AP | baseline mean/median | IO mean/median | improvement mean/median | RX | first20 vs last20 wake |
|---|---:|---:|---:|---:|---|
| chirkov | 163.0 / 150.1 | 501.6 / 504.1 | **worse** (~−208% / −236%) | 96 | ~1771 vs ~1779 ms (no progressive decay; consistently slow) |
| aethernetio | 163.9 / 153.4 | 115.4 / 114.0 | **+29.6% / +25.7%** | 100 | stable |

So IO/DirectDeepSleep is **not** a portable win; it breaks the next association
path on chirkov (~1.76 s wakes) while helping aethernetio.

### Combination study

Confirmed ≥~5% mean on chirkov: **SKIP**, **CPU** (weak alone).  
Rejected: DISC_PM_OFF, WIFI_PS_MIN, IO on chirkov.

Tested: `SKIP + CPU80` → best overall on both APs.  
SKIP alone was close; adding CPU80 improved mean slightly further on chirkov
(113.9 vs 123.0 first-pass). No evidence B2/B3 were independent wins — they were
the same SKIP_VALIDATE contamination effect in the original study.

### vs product HOT reference

Historical product HOT: chirkov median ~106 mJ / mean ~111 mJ.  
Confirmation A0: median ~150 mJ / mean ~163 mJ.

Likely contributors (not force-fit): confirmation A0 has **no** SKIP_VALIDATE;
product path historically enabled deep-sleep skip-validate; AP/RF conditions and
segmentation differ; study A0 from contaminated campaign (~118.6 mJ) was already
between product and clean confirmation A0.

## FINAL DECISION

CHIRKOV BASELINE:
- before mean/median = 176.0 / 152.4
- after mean/median = 150.1 / 147.8
- drift median = 3.0%

AETHERNETIO BASELINE:
- before mean/median = 168.2 / 153.5 (CFM10R)
- after mean/median = 159.6 / 153.3
- drift median = 0.14%

SINGLE FACTORS CONFIRMED:
- SKIP_VALIDATE: mean −24.6%, median −29.7%, RX=94
- DISC_PM_OFF: mean +52.2% (worse), median +3.5%, RX=98 → **not confirmed**
- WIFI_PS_MIN: mean +40.5% (worse), median −0.4%, RX=100 → **not confirmed**
- CPU80: mean −8.3%, median −1.9%, RX=99 → weak alone; useful in combo

IO_TEARDOWN:
- exact semantics = DirectDeepSleep (no wifi stop/deinit/handlers/netif)
- chirkov mean/median = 501.6 / 504.1; improvement = large regression; RX=96
- aethernetio mean/median = 115.4 / 114.0; improvement = −29.6% / −25.7% mean/median energy (lower is better); RX=100

BEST COMBINATION:
- settings = `CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP=y` + CPU 80 MHz (`esp_pm`), full teardown, DISCONNECTED_PM left ON, WIFI_PS_NONE
- chirkov (repeat) mean/median/p90 = 118.7 / 101.0 / 124.7; improvement mean −27.2%; RX=91
- aethernetio mean/median/p90 = 130.8 / 126.1 / 153.9; improvement mean −20.2%; RX=100

BUILD_CONTAMINATION_FOUND=yes

## Battery estimate (CR2 800 mAh nominal @ 3.0 V)

Using **MEAN** energy of best chirkov repeat: **118.7 mJ/send**.  
charge_per_send = 118.7 / 3.0 = 39.6 mC. Sleep = 8 µA.

| Period | I_avg | Idealized life |
|---|---:|---|
| 1 send / 1 min | 0.667 mA | ~50 days (~0.14 y) |
| 1 send / 10 min | 0.074 mA | ~451 days (~1.23 y) |

Note: usable CR2 capacity under Wi-Fi current pulses is often below nominal 800 mAh.

## Git / process notes

SERVER_CHANGED=no  
Raw PPK CSVs under `experiments/power_modes_raw/confirmation/` are **not** committed.
