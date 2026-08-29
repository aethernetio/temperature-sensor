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
- PRE: **winner = 25 ms** (see SHORT PRE SEARCH)
- POST: **TX_DONE_CALLBACK** (fixed delay = 0)

## CALLBACK

- Registration point: **immediately before `sendto()`**
- Socket kept open until first callback (or timeout), then unset + close + teardown
- First TX-done used; **no** fingerprint / payload match
- Wait: 50 ms primary, extend to 100 ms total max (safety only)

## RESULT (baseline PRE=200)

| Run | N | delivered | callback_seen | timeouts | connect_med | txdone_med | teardown_med | cycle_med | p90 | max |
|-----|---|-----------|---------------|----------|-------------|------------|--------------|-----------|-----|-----|
| VAL20 | 20 | 19/20 | 20/20 | 0 | 127 | 1 | 77 | **420** | 440 | 490 |
| VAL100 | 100 | 100/100 | 100/100 | 0 | 127 | 2 | 86 | **420** | 490 | 680 |
| VAL200 | 200 | 189/200 | 200/200 | 0 | 127 | 1 | 97 | **430** | 490 | 780 |

## SHORT PRE SEARCH

Only PRE changed. Screens use **N=50**. Adaptive order: 25 → 10 → 0.

| PRE | N | delivery | callback_seen | timeout | connect med | txdone med | cycle med | p90 | max | note |
|-----|---|----------|---------------|---------|-------------|------------|-----------|-----|-----|------|
| 200 | 200 | 189/200 | 200/200 | 0 | 127 | 1 | **430** | 490 | 780 | prior VAL200 |
| **25** | **50** | **37/50** | **50/50** | **0** | 128 | 13 | **230** | 250 | 310 | screen |
| **25** | **200** | **182/200** | **200/200** | **0** | 131 | 20 | **230** | 270 | 390 | **VAL200 winner** |
| 10 | 50 | 21/50 | 50/50 | 0 | 143 | 0 | 220 | 250 | 290 | delivery too weak — no VAL200 |
| 0 | 50 | 7/50 | 49/50 | 1 | 130 | 0 | 220 | 250 | 1060 | collapsed — no VAL200 |

Lifecycle on PRE25 VAL200: wifi_ready=200, encode=200, sendto=200, nonce=200. missing=18, duplicates=0, ooo=0.

### Winner

**PRE = 25 ms**, POST = late TX-done callback.

- callback_seen stable (200/200)
- callback_timeout = 0
- Wi-Fi lifecycle intact
- delivery at normal UDP level (182/200 ≈ 91%)

PRE=0 and PRE=10 are faster by ~10 ms median but delivery collapses; not winners.

## COMPARE

| | PRE | cycle median | delivery |
|--|-----|--------------|----------|
| OLD fixed POST=300 | 200 | 710 ms | 198/200 |
| Callback PRE=200 | 200 | **430 ms** | 189/200 |
| **Callback PRE=25 (winner)** | **25** | **230 ms** | **182/200** |

### vs prior callback PRE=200 (430 ms)

- Absolute saving: **200 ms**
- Percent saving: **46.5%**
- Speedup: **1.87×**

### vs original fixed-POST winner (710 ms)

- Absolute saving: **480 ms**
- Percent saving: **67.6%**
- Speedup: **3.09×**

## Raw data

See `experiments/prepared_wifi_callback_wpa2.tsv`.
Orchestrator: `experiments/run_callback_short_pre.py`.
