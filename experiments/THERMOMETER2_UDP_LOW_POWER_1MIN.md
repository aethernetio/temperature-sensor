# Thermometer 2 UDP low-power 1-minute

- Repo: `temperature-sensor-prepared (origin aethernetio/temperature-sensor)`
- Starting branch: `diag/thermometer2-sleep-power-bisect`
- Starting SHA: `a2aa419c51ada9f092662a0be39207a0551826e1`
- Branch: `diag/thermometer2-udp-low-power-1min`
- CURRENT_FLASH_EXPECTED (before reflash): `B1_LED_ON_LOW` (~7.05 µA sleep-only)
- Voltage: 3000 mV (PPK2 source meter)
- UDP: `192.168.68.84:9000` LE uint32 index
- Mode: `AETHER_DIAG_UDP_LOW_POWER_1MIN_10`
- Started: 2026-09-14T08:53:45.548750+00:00
- Updated: 2026-09-14T09:16:47.693113+00:00

## Verdict

**PASS**

Two consecutive 10/10 runs at `pre=300 ms`, `post=200 ms`. Deep-sleep after full Wi-Fi teardown ≈ **7.8–8.4 µA** (TARGET ≤16 µA).

## Results

| run | attempts | RX | loss | sleep µA | sleep med µA | total C | mC/cycle | total J | mJ/cycle | post_ms | status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| HOLD200_RUN1 | 10 | 10 | 0.00% | 8.35 | 3.00 | 6.303741 | 630.37 | 18.911222 | 1891.12 | 200 | OK |
| HOLD200_RUN2 | 10 | 10 | 0.00% | 7.83 | 3.00 | 6.012748 | 601.27 | 18.038243 | 1803.82 | 200 | OK |

CONFIRMATION: RUN1_RX=10/10, RUN2_RX=10/10 (energy from RUN2).

## CACHE
- channel=yes
- BSSID=yes
- static_ip=yes
- peer_arp=yes
- gateway_arp=yes

## HOLDS
- pre=300 ms
- post=200 ms (confirmed; no hold search needed)

## POWERDOWN
- GPIO2 PWR_ON = LOW+hold
- GPIO17 LED_ON = LOW+hold
- GPIO18 LED data = high-Z+hold
- GPIO6 SDA = high-Z+hold
- GPIO7 SCL = high-Z+hold
- ULP/LP = stopped (`BoardPowerDownForDeepSleep`)
- RTC_FAST/SLOW mem = ON (RTC_DATA_ATTR cache retained)

## Energy (clean RUN2)
- SLEEP_AVG_UA=7.83
- SLEEP_MEDIAN_UA=3.00
- TOTAL_10_CHARGE_C=6.012748
- TOTAL_10_ENERGY_J=18.038243
- AVG_CHARGE_PER_INTERVAL_mC=601.27
- AVG_ENERGY_PER_INTERVAL_mJ=1803.82
- CR2 800 mAh life≈3.33 d / 0.11 mo (1-min UDP cadence; Wi-Fi dominates)
- sleep-only mJ/cycle≈1.41
- Wi-Fi/send mJ/cycle≈1802.41

Deep-sleep current is from `|I|<200 µA` samples between bursts, not from the 10-minute overall average (which includes Wi-Fi).

## Final firmware left flashed
- mode=`AETHER_DIAG_UDP_LOW_POWER_1MIN_10`
- silent console (`CONFIG_ESP_CONSOLE_NONE`)
- pre=300 / post=200
- after 10 measured sends → DONE 1 h deep sleep

## Method
1. `AETHER_DIAG_UDP_LOW_POWER_1MIN_10` silent build
2. PPK OFF→ON 3.0 V, Espressif VID 0x303A, esptool flash
3. Warmup #0 (not measured) then 10×60 s HOT UDP
4. After send #10 include the last 60 s sleep; then DONE 1 h sleep
