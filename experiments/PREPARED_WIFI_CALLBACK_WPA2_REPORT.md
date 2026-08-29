# Prepared Wi-Fi late TX-done callback (WPA2) report

Experiment only. Production `SendPreparedOnce` was **not** switched.

## Pins

| Repo | Branch / note | SHA |
|------|---------------|-----|
| temperature-sensor | `thermometer-prepared-send-v0` | *(this commit)* |
| aether-client-cpp | unchanged | `157aadbec8e7b852d0f89274307ff7cb8103e5f7` |

## CONFIG

- Negotiated auth: **WPA2_PSK** (`authmode=3`) via benchmark-only `CONFIG_ESP_WIFI_ENABLE_WPA3_SAE=n`
- Channel cache: **yes**
- BSSID cache: **no**
- Static IPv4 / netmask / gateway: **yes**
- Cached gateway MAC + static ARP: **yes**
- Wi-Fi 4 only (`11B|11G|11N`), no 802.11ax
- PHY: automatic rate adaptation
- `WIFI_PS_NONE`, max TX power, max normal ESP32-C6 CPU freq
- Association retry: **10**
- PRE: **200 ms** for VAL*; best short screen PRE: **25 ms**
- POST: **TX_DONE_CALLBACK** (no fixed 300 ms)

## CALLBACK

- Registration point: **immediately before `sendto()`**
- Socket kept open until first callback (or timeout), then unset + close + teardown
- First TX-done used; **no** fingerprint / payload match
- Wait: 50 ms primary, extend to 100 ms total max
- `callback_timeout` counted separately (VAL*: **0**)

## RESULT

| Run | N | delivered | callback_seen | timeouts | connect_med | txdone_med | teardown_med | cycle_med | p90 | max |
|-----|---|-----------|---------------|----------|-------------|------------|--------------|-----------|-----|-----|
| VAL20 | 20 | 19/20 | 20/20 | 0 | 127 | 1 | 77 | **420** | 440 | 490 |
| VAL100 | 100 | 100/100 | 100/100 | 0 | 127 | 2 | 86 | **420** | 490 | 680 |
| VAL200 | 200 | 189/200 | 200/200 | 0 | 127 | 1 | 97 | **430** | 490 | 780 |

Lifecycle on VAL200: wifi_ready=200, encode=200, sendto=200, nonce=200.

### PRE sweep (screen ×20, POST=callback only)

| PRE | delivered | callback | cycle_med | note |
|-----|-----------|----------|-----------|------|
| 150 | 20/20 | 20/20 | 350 | ok |
| 100 | 20/20 | 20/20 | 320 | ok |
| 75 | 20/20 | 20/20 | 280 | ok |
| 50 | 16/20 | 20/20 | 270 | weaker delivery |
| **25** | **20/20** | **20/20** | **230** | **shortest practical** |
| 0 | 3/20 | 20/20 | 220 | delivery collapsed — stop |

## COMPARE (VAL200 PRE=200)

| | cycle median | delivery |
|--|--------------|----------|
| OLD winner (PRE=200, POST=300, WPA2) | **710 ms** | 198/200 |
| NEW callback (PRE=200, POST=cb) | **430 ms** | 189/200 |

- Absolute saving: **280 ms**
- Percent saving: **39.4%**
- Speedup: **1.65×**

Callback path is the new fastest candidate at PRE=200. Shortest practical PRE from sweep: **25 ms** (screen 20/20 @ 230 ms cycle); not re-validated at N=200 in this campaign.

## Raw data

See `experiments/prepared_wifi_callback_wpa2.tsv`.
