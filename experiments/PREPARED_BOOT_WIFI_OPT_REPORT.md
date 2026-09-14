# Prepared Boot / Wi-Fi HOT Optimization Report

ESP32-C6 · ESP-IDF v6.0.2 · branch `thermometer-prepared-send-v0`  
aether-client-cpp **unchanged** `157aadbec8e7b852d0f89274307ff7cb8103e5f7`

## Campaign

- One autonomous flash, RTC state machine, **30 HOT / variant** (not 50).
- Telemetry only via Æther (`BootWifiOptPayload` `0xD8`); no COM after flash.
- Baseline locked: WPA2, cached channel, no BSSID, static IP/ARP, Wi-Fi 4, auto PHY,
  `WIFI_PS_NONE`, default TX power, PRE=25 ms, late TX-done, no MAC retry setter,
  deep sleep between sends.
- Runtime matrix: **D0–D3, G1, H1–H2, E1–E4** (11 variants).
- Stopped at **E4 ≈27/30** (COM stuck awake); **298 HOT** records analyzed. FINAL not received.

## Effective compile-time config (this flash)

| Item | Value | Notes |
|------|-------|-------|
| External RTC crystal | **yes** `CONFIG_RTC_CLK_SRC_EXT_CRYS=y` | Already enabled |
| `RTC_CLK_CAL_CYCLES` | **1024** | Defaults file asks for `0`; forced `0` on first attempt left COM awake with zero telemetry — **reverted** |
| Boot validation | `SKIP_VALIDATE_IN_DEEP_SLEEP=y` (+ `ON_POWER_ON=y`) | A1 already; A0 not a separate flash |
| Bootloader opt | **SIZE** (not PERF) | A2 skipped (one-flash) |
| Flash | **DIO 80 MHz** | QIO not forced (board/flash not verified for QIO) |
| Secure Boot | **disabled** | |
| Flash encryption | **disabled** | |
| AMPDU TX/RX | **already off** in sdkconfig | F1–F6 skipped as identical to control |

### A-matrix (one-flash limitation)

| Variant | Status |
|---------|--------|
| A0 CONTROL (validate on deep-sleep wake) | Not flashed separately |
| A1 SKIP_VALIDATE_IN_DEEP_SLEEP | **Effective** |
| A2 + PERF | Not applied |
| A3 QIO 80 | Not applied (DIO 80 kept) |
| A4 CAL_CYCLES=0 | **Attempted → hang; kept 1024** |
| A5 SKIP_VALIDATE_ALWAYS | Not applied (bench-only; Secure Boot off) |

**Security note (A5):** `SKIP_VALIDATE_ALWAYS` disables image validation on every boot — incompatible with Secure Boot / anti-rollback / flash-encryption production posture. Effective here: Secure Boot off, flash encryption off.

### B — External crystal

Enabled. Wake overhead median ≈ **40.7 ms** on all variants (compile-time, not runtime).  
3 s sleep drift / hardware deep-sleep current: not measured in software (scope separately).

### C — PHY cal RTC reuse

**`RTC_PHY_CAL_REUSE_NOT_SUPPORTED_BY_PUBLIC_API`**

IDF 6.0.2 exposes `esp_phy_load_cal_data_from_nvs` / `esp_phy_store_cal_data_to_nvs` and deep-sleep path uses `PHY_RF_CAL_NONE`, but there is **no public API** to inject an RTC-held `esp_phy_calibration_data_t` blob into PHY init. No opaque driver memcpy. Measured path = stock IDF deep-sleep no-calibration + NVS cal storage left enabled.

## Main results (median µs)

See also `experiments/prepared_boot_wifi_opt_summary.tsv` and raw `experiments/prepared_boot_wifi_opt.tsv`.

| variant | setting | delivery/30 | wake_ov_med | wifi_init_med | connect_med | txdone_med | hot_user_med | p90 | max | heap_delta | notes |
|---------|---------|-------------|-------------|---------------|-------------|------------|--------------|-----|-----|------------|-------|
| D0_CONTROL | baseline | 29/30 | 40732 | 16447 | 143814 | 6490 | 250269 | 320267 | 410269 | 46584 | n=29 |
| D1_STORAGE_RAM | WIFI_STORAGE_RAM | 27/30 | 40736 | 16404 | 138933 | 5606 | **240270** | 310257 | 520259 | 46584 | n=27 |
| D2_NVS_OFF | nvs_enable=0 | 25/30 | 40770 | **10623** | 233508 | 7721 | 340259 | 380256 | 470264 | 46480 | worse connect |
| D3_RAM_NVS_OFF | RAM+nvs_off | 25/30 | 40772 | 10618 | 238533 | 6435 | 370256 | 420256 | 440249 | 46480 | worse |
| G1_HT20 | force HT20 | 30/30 | 40752 | 16360 | 134263 | 6306 | 250271 | 320269 | 390269 | 46584 | connect↓ |
| H1_CS_OFF | dynamic_cs=false | 26/30 | 40767 | 16441 | 138312 | 5037 | 250264 | 310271 | 380266 | 46584 | ≈D0 |
| H2_CS_ON | dynamic_cs=true | 26/30 | 40776 | 16357 | 141900 | 5552 | 270274 | 320272 | 820265 | 46584 | worse tails |
| E1_TX_HALF | dyn_tx=16 | 30/30 | 40772 | 16433 | 133819 | 3138 | 260266 | 310269 | 670262 | 46584 | time≈ |
| E2_TX_MIN | dyn_tx=8 | 25/30 | 40766 | 16366 | 136230 | 4665 | 250269 | 310269 | 340269 | 46584 | time≈ |
| E3_RX_HALF | rx 5/16 | 29/30 | 40768 | 16366 | **132388** | 5142 | 250269 | 310274 | 1040297 | **37984** | RAM↓ |
| E4_RX_MIN | rx 3/8 | 26/30 | 40780 | 16272 | 146044 | 5960 | 270269 | 420267 | 770272 | **34544** | best RAM |

## Winners

| Category | Winner | Evidence |
|----------|--------|----------|
| **BOOT** | A1 + EXT_CRYS (wake_ov ≈40.7 ms) | Flat across runtime variants; A4=0 unsafe here |
| **WIFI / hot_user** | **D1_STORAGE_RAM** | −10 ms median vs D0 |
| **RAM** | **E4_RX_MIN** (then E3) | heap_delta 34544 / 37984 vs 46584 |
| **PHY CAL** | N/A public RTC inject | stock IDF no-cal on deep-sleep wake |
| **EXTERNAL XTAL** | enabled | CAL_CYCLES effective **1024** |
| **Wi-Fi storage** | **D1 RAM** | D2/D3 hurt connect ≫ init win |
| **NVS enable** | keep **enabled** | nvs_off regresses hot cycle |
| **AMPDU/A-MSDU** | already off | no new variants |
| **HT20** | neutral time, slightly faster connect | include in combined |
| **dynamic CS** | **false** (H1) ≈ control; true worse tails | include false |
| **buffers** | E3/E4 save RAM; little time win | E3 in combined |

### Combined BEST candidate → VAL100

`WIFI_STORAGE_RAM` + `force HT20` + `dynamic_cs=false` + RX half (`static_rx=5`, `dynamic_rx=16`) + `nvs_enable=1`.

TX power / MAC retry policy unchanged.

## VAL100

Combined: `WIFI_STORAGE_RAM` + HT20 + `dynamic_cs=false` + RX half (5/16) + `nvs_enable=1`.

| metric | value |
|--------|-------|
| delivery | **91/100** received (txok=91); FINAL received |
| wake_overhead_med | **40710 µs** |
| wifi_init_med | 16387 µs |
| connect_med | **131618 µs** |
| txdone_med | 11621 µs |
| hot_user_med | **250269 µs** (p90 320268, max 470270) |
| heap_delta_med | **37984** (E3-class RAM) |
| brownouts | **0** |

Notes: first post-flash wake needed a manual board reset (COM stuck awake until reset), same as the main campaign. Combined config matches D0 on hot_user median; **D1 alone** remains the best single time win (−10 ms). Combined keeps E3 RAM savings and faster connect vs D0.

Raw: `experiments/prepared_boot_wifi_val100.tsv`
