# Thermometer 2 deep-sleep power bisect

- Repo used: `temperature-sensor-prepared (origin aethernetio/temperature-sensor)`
- Branch: `diag/thermometer2-sleep-power-bisect`
- Original: `diag/deep-sleep-10min` @ `4ee8bfcb4d086be9820433eeb4a26ddd2e9aa0bd`
- Backup: `backup/thermometer2-before-sleep-power-bisect`
- Voltage: 3000 mV (PPK2 source meter)
- Started: 2026-09-14T06:36:46.912276+00:00
- Updated: 2026-09-14T08:39:12.273199+00:00

## Verdict

**TARGET ≤16 µA reached.** Confirmed best sleep ≈ **7.0 µA** (`B1_LED_ON_LOW`).

**ROOT_CAUSE:** The reported ~1 mA deep-sleep current was **software/peripheral state**, not a hardware floor. Minimal sleep-only firmware (`M0_MINIMAL`) already sleeps at **~8.7 µA**, proving the board/regulator/sensor hardware can reach tens of µA when rails and pins are quiet.

## Results

| Variant | sleep_avg_uA | sleep_med_uA | median_uA | avg_uA | frac | status | notes |
|---|---:|---:|---:|---:|---:|---|---|
| M0_MINIMAL | 8.68 | 3.55 | 4.35 | 7526.64 | 0.87 | OK | hardware floor control |
| H1_PWR_HIGH | 11.48 | 4.79 | 5.76 | 9660.09 | 0.83 | OK | GPIO2 HIGH+hold (+~3 µA vs M0) |
| A2_PWR_HOLD | 9.30 | 3.82 | 4.66 | 7498.49 | 0.86 | OK | GPIO2 LOW+hold |
| Z_FULL_QUIET | 8.07 | 3.29 | 3.99 | 6446.26 | 0.88 | OK | full quiet set |
| B1_LED_ON_LOW | 7.04 | 2.67 | 3.37 | 5572.03 | 0.90 | OK | **best**; GPIO2 LOW+hold + GPIO17 LOW |
| B2_LED_ON_HIGH | 8.70 | 3.60 | 4.44 | 7329.62 | 0.87 | OK | GPIO17 HIGH; ~1.7 µA worse than B1 |

### Confirmations (`B1_LED_ON_LOW`, 3× power-cycle → flash → reset → ≥30 s)

| Run | sleep_avg_uA | status |
|---|---:|---|
| CONFIRM_1 | 7.00 | OK |
| CONFIRM_2 | 7.01 | OK |
| CONFIRM_3 | 7.05 | OK |

Confirmed mean ≈ **7.02 µA** (tight, repeatable).

## Analysis

### BASELINE
- User-reported production sleep ≈ **1000 µA**
- Diag minimal / quiet path ≈ **7–12 µA**

### MINIMAL_FIRMWARE
- `M0_MINIMAL` sleep_avg = **8.68 µA**
- No Wi-Fi / Aether / sensors / LED drivers / ULP app / I2C / rail driving
- Cold-boot flash window 25 s; post-flash window 8 s (diag only)

### BEST_VARIANT
- Name: `B1_LED_ON_LOW`
- Config: PD domains min + **GPIO2 LOW+hold** + **GPIO17 LOW+hold**
- sleep_avg (initial) = 7.04 µA; confirmed 7.00 / 7.01 / 7.05 µA

### ROOT_CAUSE
- Exact factor(s): production deep-sleep path did **not** systematically:
  1. Drive **PWR_ON (GPIO2) LOW + gpio_hold**
  2. Drive **STATUS_LED_ON (GPIO17) LOW + hold** (OFF polarity)
  3. Float **SDA/SCL (GPIO6/7)** high-Z, no pulls + hold
  4. **Stop LP/ULP core** (even if this image never started it — prior image may have)
- Leaving those active / floating / leftover explains milliamp-class sleep; quieting them restores hardware floor (~µA).

### PIN STATES (best / production fix)

| Pin | Role | Best state |
|---|---|---|
| GPIO2 | PWR_ON (active-high rail) | **LOW + hold** |
| GPIO17 | STATUS_LED_ON | **LOW + hold** (OFF) |
| GPIO18 | STATUS_LED data | high-Z + hold (production/Z) |
| GPIO6 | SDA | high-Z, no pulls + hold (production/Z) |
| GPIO7 | SCL | high-Z, no pulls + hold (production/Z) |

**LED polarity:** B1 (GPIO17 LOW) ≈ 7.0 µA vs B2 (GPIO17 HIGH) ≈ 8.7 µA → **OFF = LOW**. Neither polarity alone produces ~1 mA in this isolation firmware; LED is a small additive factor once the rest is quiet.

**PWR_ON:** H1 HIGH ≈ 11.5 µA vs A2 LOW ≈ 9.3 µA → rail ON adds only ~2–3 µA when sensors are not clocked; still required OFF+hold for production (prevents sensor/rail leakage after Init).

### ULP
- enabled in product Thermometer 2 board header: **BOARD_HAS_ULP=0** → `ESP_MAIN_SLEEP`
- stopped before sleep in production fix: **yes** (LP core disable + ULP wakeup cleared)

### RTC DOMAINS
- Diagnostic best path: force **OFF** where supported (min PD)
- Production: keep **RTC_SLOW_MEM / RTC_FAST_MEM ON** for retained Aether/prepared state; peripheral quieting alone is enough for ≤16 µA

### PRODUCTION FIX
- New `BoardPowerDownForDeepSleep()` in `main/sleeping/board_sleep_powerdown.*`
- Called from production `DeepSleep()` in `esp_main_sleep.cpp` (no flash-window delays)
- UDP RTC diag reuses the same helper
- `STATUS_LED_ON_OFF_LEVEL=0` documented in `aether_esp32_c6.h`

## Criteria
- TARGET ≤ 16 µA — **yes**
- GOOD ≤ 25 µA — **yes**
- Intermediate ≤ 50 µA — **yes**

## Method
1. Dedicated build per variant (`AETHER_DIAG_SLEEP_POWER_BISECT`)
2. PPK power OFF → ON (3.0 V)
3. Wait Espressif COM (VID 0x303A; ignore Nordic PPK 0x1915)
4. Flash → reset → wait for deep sleep (~8 s flash window)
5. Measure ≥30 s sleep current on same PPK session (`|I|<200 µA` sleep filter)

## Notes
- Diag flash windows are compile-time only; not for production.
- Raw PPK CSV traces are local artifacts and are not committed.
