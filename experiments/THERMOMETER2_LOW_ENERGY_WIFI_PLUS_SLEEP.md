# Thermometer 2 — low-energy Wi-Fi + 7 µA sleep

> **SUPERSEDED / MEASUREMENT BUG**
> Previous sleep value ~8.5 µA was invalid due to sample-threshold filtering (`ua < 200`). Manual PPK contiguous window showed **~8.41 mA** (343.16 mC / 40.79 s). See `THERMOMETER2_COMBINED_SLEEP_ROOT_CAUSE.md`. Sleep-only B1 “7 µA” used the same filter; the bisect `avg_uA` column was already ~mA. Under raw contiguous measurement, B1 and combined are both ~7–9 mA mean / ~µA median.

- Branch: `diag/thermometer2-lowenergy-wifi-plus-7ua-sleep`
- SHA: `494260d5a87e7dd4e43afd813d4ea4cd9d9c75a7`
- Parent/historical repro SHA: `14118bac5570a2bba0d311197799faed0a63b250`
- Voltage: 3000 mV
- UDP: `192.168.68.84:9000`

## 1. Historical low-energy source

```
AETHER_DIAG_UDP_RTC_INDEX @ force_sdk; PRE=50ms POST=200ms; 10s sleep; full RTC cache; WiFi4+1M; PS_NONE then MAX_MODEM; FULL teardown; UDP 192.168.68.84:9000
```

- Closest documented numeric match also: LONG1000_CHIRKOV prepared HOT
  (~56.8 mC / ~572 ms) on feat/prepared-power-factor-study.
- Thermometer2 UDP path reproduced here: `AETHER_DIAG_UDP_RTC_INDEX`
  with `force_sdk` from `_diag_udp_rtc_index_flash.py`, PRE=50 POST=200,
  10 s interval, full RTC cache, Wi-Fi4+1M, FULL teardown, **no** BoardPowerDown.

## 2. Exact reproduction (A)

| Q | start s | end s | dur ms | mC |
|---|---:|---:|---:|---:|
| 1 | 10.420 | 11.340 | 920 | 60.38 |
| 2 | 21.020 | 21.910 | 890 | 55.26 |
| 3 | 31.590 | 32.480 | 890 | 55.93 |
| 4 | 42.160 | 44.360 | 2200 | 192.20 |
| 5 | 53.990 | 54.910 | 920 | 55.40 |
| 6 | 64.580 | 65.560 | 980 | 63.24 |
| 7 | 75.230 | 76.220 | 990 | 62.86 |
| 8 | 85.870 | 86.770 | 900 | 53.72 |
| 9 | 96.450 | 97.360 | 910 | 56.57 |
| 10 | 107.040 | 107.930 | 890 | 55.68 |

- RX: idx 0..14 (15 packets); 10 measured wakes after warm/power-on
- mean/median/min/max mC: 71.12561956994855/56.25069002869783/53.71947524190194/192.20339851970343
- median duration ms: 915.0000000000053
- acceptance 40–80 mC: **True**

## 3. Visual PPK boundary method

- Reconstruct sample time at 5 kHz (decim 20 from 100 kHz).
- 100 Hz envelope: active if I>1.5 mA for ≥150 ms.
- Pad −80 ms / +200 ms for rise + teardown tail into deep-sleep floor.
- Do **not** stop at sendto / wifi_stop; include visible tail.
- Overview: `experiments\power_factor_results\thermometer2_low_energy_plots\historical_10_wakes_overview.png`

## 4. Sleep cleanup-only diff

- Same Wi-Fi/UDP algorithm (PRE=50, POST=200, caches, PS, teardown, 1M).
- Added/kept `BoardPowerDownForDeepSleep()` before `esp_deep_sleep_start`.
- Interval changed only for final run: 60 s start-to-start, 10 measured sends.
- Default PRE for `AETHER_DIAG_UDP_LOW_POWER_1MIN_10` set to 50 (was 300).
- RTC_DATA_ATTR caches retained; console NONE for sleep measurement.

## 5. Final combined 10-send run (B)

| Q | start s | end s | dur ms | mC |
|---|---:|---:|---:|---:|
| 1 | 11.450 | 12.280 | 830 | 52.79 |
| 2 | 71.120 | 72.000 | 880 | 55.75 |
| 3 | 130.830 | 131.650 | 820 | 52.34 |
| 4 | 190.550 | 191.380 | 830 | 52.76 |
| 5 | 250.320 | 251.150 | 830 | 53.75 |
| 6 | 310.060 | 310.890 | 830 | 53.20 |
| 7 | 369.810 | 370.660 | 850 | 54.56 |
| 8 | 429.580 | 430.430 | 850 | 53.10 |
| 9 | 489.330 | 490.160 | 830 | 51.60 |
| 10 | 549.040 | 549.870 | 830 | 53.06 |

- mean/median active mC: 53.29094220406846/53.08314901574116
- sleep avg/median µA: 8.513555384836991/3.331
- mean charge per 1-min cycle mC: 53.79462116774618
- overview: `C:\Users\nickc\Projects\temperature-sensor-prepared\experiments\power_factor_results\thermometer2_low_energy_plots\combined_10_wakes_overview.png`

## 6. Delivery

- Combined UDP summary: {"warmup_rx": 1, "attempts": 10, "rx_unique": 10, "rx_total": 10, "missing": [], "duplicates": {}, "all_idx": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10], "t_idx": {"0": 1789379897.8782575, "1": 1789379906.2976694, "2": 1789379966.0151954, "3": 1789380025.6628542, "4": 1789380085.3954298, "5": 1789380145.1636853, "6": 1789380204.9017916, "7": 1789380264.6763105, "8": 1789380324.4373317, "9": 1789380384.1684356, "10": 1789380443.877817}}

## 7. Battery estimate (CR2 800 mAh @ 3.0 V)

- active mean mC/send: 53.29094220406846
- sleep µA: 8.513555384836991
- total mC / 1 min: 53.79462116774618
- I_avg µA: 896.58
- life: 37.2 d / 1.24 mo

## A/B

| Metric | A old Wi-Fi | B + low-power sleep |
|---|---:|---:|
| RX | see hist | 10 |
| active mean mC | 71.12561956994855 | 53.29094220406846 |
| active median mC | 56.25069002869783 | 53.08314901574116 |
| wake duration mean ms | 1049.0000000000032 | 838.0000000000039 |
| sleep current µA | 10.311466367003115 | 8.513555384836991 |

