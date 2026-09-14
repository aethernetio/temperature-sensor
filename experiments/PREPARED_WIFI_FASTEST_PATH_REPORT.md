# Prepared Wi-Fi Fastest Path Report (ESP32-C6)

## Pins
- **temperature-sensor** branch: `thermometer-prepared-send-v0`
- **aether-client-cpp**: `157aadbec8e7b852d0f89274307ff7cb8103e5f7` **unchanged=yes**
- ESP-IDF v6.0.2 · Silent Release / NDEBUG · `CONFIG_ESP_CONSOLE_NONE` · log level NONE
- CPU 160 MHz · `# CONFIG_PM_ENABLE is not set` · `WIFI_PS_NONE` · max TX power · Wi-Fi 4 only · auto PHY
- Pattern: full Wi-Fi init → associate → prepared UDP → full teardown → 1 s gap **outside** timer
- No deep/light sleep / reboot during benchmark
- Production `SendPreparedOnce` **not** switched to winner

## OLD vs NEW
| | |
|---|---|
| OLD | ~850 ms (prior C6/C8 static-IP cycle) |
| NEW | **710 ms** (VAL200 median cycle) |
| Absolute saving | **140 ms** |
| Percent saving | **16.5%** |
| Speedup | **1.20×** |

## BEST RELIABLE CONFIG (winner)
Effective measurement configuration:

| Knob | Value |
|------|-------|
| Wi-Fi protocol | 802.11b/g/n only |
| Channel cache | yes |
| BSSID cache | **no** |
| Static IPv4 + netmask + gateway | yes |
| Static ARP (gateway MAC) | yes |
| Scan method | default (not `WIFI_FAST_SCAN`) |
| Auth | **WPA2-PSK** (negotiated `authmode=3`) |
| SAE / WPA3 | benchmark-only `CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=n` (restored after tests; **security decision deferred**) |
| Association retry max | **10** |
| PRE-send delay | **200 ms** (after network-ready) |
| POST-send | **300 ms** fixed hold |
| TX-done callback | **not used** |
| AMPDU TX | already off in sdkconfig; explicit D1 no cycle win |
| Wi-Fi storage | NVS default (`WIFI_STORAGE_RAM` not adopted) |
| IRAM | `CONFIG_ESP_WIFI_IRAM_OPT` / `RX_IRAM_OPT` already on; `CONFIG_LWIP_IRAM_OPTIMIZATION` off (not A/B'd — sendto not bottleneck) |

### VAL200 (winner validation)
- **Delivered: 198/200** (not rounded)
- connect median **125 ms** · cycle median **710 ms** · p90 **750** · max **870**
- wifi_ready / encode / sendto / nonce = 200/200

## Callback verdict
- IDF 6.0.2 API: `esp_wifi_set_tx_done_cb` (`esp_private/wifi.h`)
- CB0 (POST=0): callback **fires** (`cb_any=18`) but **fingerprint match=0**; delivery **18/20**
- **CALLBACK_NOT_USABLE** for normal lwIP/BSD UDP — do not pursue further

## Auth (WPA2 / WPA3 / H2E)
- AP advertises transition (`authmode=7` on WPA3-capable builds)
- Threshold-only WPA2 still negotiated as 7 while SAE enabled
- With `CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=n`: negotiated **authmode=3 (WPA2_PSK)**; connect ~128 ms; cycle ~710 ms @ PRE/POST 200/300
- H2E-only: no clear win; delivery 18/20 on screen
- **Report only** — production security not auto-weakened; SAE restored in build tree after campaign

## Association extras (from BASE WPA3 path)
- A1 BSSID / A2 FAST_SCAN / A3 both: no clear ≥20 ms improvement; some delivery loss

## Retry
- R0/R1/R3 success-path medians similar; R0 unreliable on longer runs (e.g. PRE_200 + retry=0 → 13/20)
- Keep **retry=10**

## Delay search
| Stage | Result |
|-------|--------|
| PRE | 200 reliable on screen; 150+POST300 → 14/20 (stop) |
| POST | 150 screen 20/20 @ ~550 ms but VAL100 only 94/100; **100 → 13/20** (stop) |
| 2D | `PRE=150,POST=200` screen 20/20 @ 540 ms; VAL100 97/100 |
| Reliability | Aggressive delays fail ≥99/100; **SAFE 200/300** best long-run delivery |

### VAL100 candidates (actual delivery)
| Variant | Delivered | Cycle med | Connect med | p90 |
|---------|-----------|-----------|-------------|-----|
| PRE200/POST150 | 94/100 | 560 | 131 | 620 |
| PRE150/POST200 | 97/100 | 560 | 131 | 610 |
| SAFE PRE200/POST300 | 98/100 | 730 | 138 | 770 |
| SAFE repeat | 96/100 | 720 | 124 | 750 |
| MID PRE200/POST200 | 91/100 | 620 | 125 | 660 |
| FAST PRE200/POST150 | 79/100 | 560 | 122 | 600 |
| FAST PRE150/POST200 | 84/100 | 540 | 118 | 580 |

Winner chosen by long-run delivery (then VAL200), not screen-only 550 ms.

## AMPDU / A-MSDU
- sdkconfig already `# CONFIG_ESP_WIFI_AMPDU_TX_ENABLED is not set` (RX also off)
- D1 explicit AMPDU TX off (correct n=20): **17/20**, cycle **710** — no improvement vs winner
- A-MSDU TX: not enabled in sdkconfig — no separate test

## STORAGE_RAM
- Screen: **19/20**, cycle **670** / connect **112** (−40 ms vs 710)
- Not adopted: delivery <20/20 on screen; no 100/200 validation; winner stays NVS storage

## sdkconfig notes
- Useful already-on: Wi-Fi IRAM opts, CPU 160 MHz, PM disabled for this campaign
- Candidate left untested (latency unclear / not bottleneck): `CONFIG_LWIP_IRAM_OPTIMIZATION`
- `CONFIG_ESP_WIFI_NVS_ENABLED=n` not tried (avoid touching Aether NVS)

## Artifacts
- TSV: `experiments/prepared_wifi_fastest_path.tsv`
- Orchestrators: `experiments/run_fastest_path.py`, `run_fastest_continue*.py`, `run_fastest_safe_val.py`
- Progress: `experiments/fastest_chat.txt`, `experiments/fastest_progress.log`
