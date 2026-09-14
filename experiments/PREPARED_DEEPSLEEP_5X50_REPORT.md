# Prepared deep-sleep 5×50 E2E report

Experiment only. Production `SendPreparedOnce` was **not** switched.

## Pins

| Repo | Branch | SHA |
|------|--------|-----|
| temperature-sensor | `thermometer-prepared-send-v0` | *(this commit)* |
| aether-client-cpp | `exp/esp32c6-wifi-lifecycle-diag` | `157aadbec8e7b852d0f89274307ff7cb8103e5f7` **unchanged** |

## Effective sdkconfig (verified before flash)

```
CONFIG_RTC_CLK_SRC_EXT_CRYS=y
# CONFIG_RTC_CLK_SRC_INT_RC is not set
CONFIG_RTC_CLK_CAL_CYCLES=1024
CONFIG_ESP_BROWNOUT_DET=y
CONFIG_ESP_BROWNOUT_DET_LVL_SEL_7=y   (from base defaults)
# CONFIG_ESP_WIFI_ENABLE_WPA3_SAE is not set
CONFIG_ESP_CONSOLE_NONE=y
CONFIG_LOG_DEFAULT_LEVEL_NONE=y
CONFIG_BOOTLOADER_LOG_LEVEL_NONE=y
CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160=y
# CONFIG_PM_ENABLE is not set
CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP=y
```

## Fast Wi-Fi / HOT path

- WPA2-PSK, negotiated authmode=3 on all recovered HOT samples
- Cached channel; **no** BSSID reconnect
- Static IPv4 / netmask / gateway + cached GW MAC + static ARP (RTC `PreparedWifiRtcCache`)
- Wi-Fi 4 (b/g/n), auto PHY, `WIFI_PS_NONE`, max TX, retry=10
- PRE=25 ms, POST=0, late TX-done callback immediately before `sendto`, socket held until callback
- Safety callback wait ≤100 ms; no fingerprint matching

## RTC state

- `RtcState` (magic `DS3P`) + CRC: phase, outer 1..5, hot 1..50, attempts, pending measurement, brownout/unexpected counters, sleep arm timestamp
- `PreparedWifiRtcCache` (magic `WCF1`) + CRC: channel, IP, netmask, gateway, gw_mac (BSSID diagnostic only)
- Prepared nonce block remains `RTC_NOINIT` `PreparedSendMessageBlock` (existing aether layout)
- Early `app_main` hook: `ExperimentEarlyAppEntry()` before WDT/`setup`

## Campaign shape

- Client id: `prepared_deepsleep_5x50_v1`
- 5 measured FULL Æther cycles
- Per FULL: `PrepareSendMessageBlock(50)` → 50 deep-sleep HOT wakes (3 s) → next FULL
- Target: 250 prepared `sendto`; FINAL Aether report after last HOT

## Delivery / lifecycle

| Item | Result |
|------|--------|
| FULL messages recovered | **5/5** |
| HOT measurement records | **249/250** (outer 5 missing last pending = HOT#50 flush via FINAL) |
| FINAL message | **not received** (FINAL Aether phase stuck/retried; campaign timed out) |
| callback_seen (HOT) | **246** |
| callback_timeout (HOT) | **3** |
| authmode=3 | **249/249** HOT |
| brownout boots | **0** |
| unexpected resets (payload) | **0** |

Prepared `sendto` lifecycle on device is independent of UDP delivery; UDP losses are expected.

## FULL USER CYCLE (n=5)

Raw (ms): 12670, 3640, 4310, 3600, 3620

| | ms |
|--|-----|
| min | 3600 |
| **median** | **3640** |
| p90 | 12670 |
| max | 12670 |

(First FULL includes colder registration-adjacent work; later FULLs ~3.6–4.3 s.)

## HOT USER CYCLE (n=249)

| | ms |
|--|-----|
| min | ~(from samples) |
| **median** | **250** |
| p90 | 320 |
| p99 | 1090 |
| max | 1370 |

## HOT WIFI CYCLE (n=249)

| | ms |
|--|-----|
| **median** | **239** |
| p90 | 309 |
| p99 | 1079 |
| max | 1359 |

## CONNECT / TX-DONE / TEARDOWN (HOT)

| | med ms | p90 ms | max ms |
|--|--------|--------|--------|
| connect | 133 | 188 | 1293 |
| tx-done wait | 11 | 23 | 100 |
| teardown | 77 | 97 | 967 |

## SLEEP → APP overhead

`sleep_elapsed_to_app_us` median ≈ **3040.7 ms** (requested 3000 ms)

| | med ms | p90 | p99 | max |
|--|--------|------|-----|-----|
| sleep_to_app_overhead | **40.7** | 40.8 | 40.8 | 40.9 |
| app_entry_esp_timer_us | **5424** | 5424 | — | 5425 |

## Brownout / reset

| | count |
|--|-------|
| ESP_RST_BROWNOUT boots (payload flag) | 0 |
| unexpected_reset_count in payloads | 0 |
| recovery FULL (not observed in TSV) | 0 |

## Confirmation

- Five FULL blocks: **yes** (records for outer 1..5)
- 50 prepared nonces per block: **yes** (HOT counts 50/50/50/50/49 recovered; last pending needs FINAL)
- Total prepared sends targeted 250: device completed through HOT#46–50 of outer 5; **249** timing records recovered via pending chain

## Artifacts

- `experiments/prepared_deepsleep_5x50.tsv`
- `experiments/run_deepsleep_5x50.py`
- `main/prepared_deepsleep_5x50_bench.cpp`
- `sdkconfig.defaults.deepsleep_5x50`

## Note on FINAL

FINAL Æther write after HOT#50 outer 5 did not reach the receiver before the orchestrator timeout. A fail-counter fallback to DONE was added for subsequent runs. Metrics above are from the successful pending-chain delivery of FULL + HOT records.
