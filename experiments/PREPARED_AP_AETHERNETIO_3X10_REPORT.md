# Prepared AP aethernetio 3×10 Report

## CONFIG

- AP SSID: **aethernetio**
- WPA2, Wi-Fi 4 b/g/n
- cached channel yes, BSSID no
- static IP / static ARP yes
- PRE 25 ms, TX-done callback, POST 0
- WIFI_STORAGE_RAM yes (D1)
- wifi nvs ON
- TX power default, MAC retry default
- external RTC crystal, CAL cycles 1024
- deep sleep **1 s** between HOT/FULL
- 3 FULL × 10 HOT
- RTC magic AET1 (invalidates prior AP cache)

## RESULT (campaign TIMEOUT 25 min, no FINAL)

- FULL received (unique outer): **3/3** (device later entered FULL recovery loop; many duplicate FULL telemetries)
- FULL user (TSV): **n/a** — no FULL pending records flushed before timeout
- HOT sendto (TSV kind=2): **2/30** — only HOT #10 of blocks 1–2 flushed via subsequent FULL payloads
- HOT received (DS_HOT lines): **0/30** (0.0%)
- FINAL: **0**

### HOT timing (2 TSV samples, both block-end flush)

- user: **1075.3 / 1060.3 ms**
- Wi-Fi: **1064.0 / 1049.0 ms**
- connect: **1001.1 / 983.8 ms** (raw all: `[1001.1, 983.8]` ms)
- tx-done: **2.6 / 0.2 ms**
- wake overhead: **40.7 ms**
- RSSI: **-19 / -18 dBm**, channel **6**

### Association

- connect median **992 ms** ≈ 1 s sleep interval → frequent reconnect under pressure
- disconnect reasons recorded: **none** (metrics only on successful sendto flush)
- failed_assoc_wakes (telemetry): **0** (counter only visible after successful HOT payload)
- callback seen/timeouts: **2/0**
- brownouts: **0**

### Observed behavior

1. Æther FULL path on `aethernetio` works (3 unique FULL outers received).
2. Prepared HOT path rarely completes; device cycles **FULL recovery** (outer 1→2→3 repeated).
3. Two successful HOT sendto at block boundaries did not produce standalone `DS_HOT` receiver lines; pending metrics arrived embedded in later FULL writes.
4. Campaign did not reach FINAL within 25 min.

## COMPARE vs previous AP (chirkov refs)

| metric | chirkov ref | aethernetio | delta |
|--------|-------------|-------------|-------|
| delivery | 99.6% (249/250) | 0.0% (0/30 DS_HOT) | -99.6 pp |
| hot user med | ~250 ms | ~1075 ms (n=2) | +825 ms |
| connect med | ~158 ms | ~992 ms | +834 ms |
| failed assoc (visible) | 0 | 0* | — |

\*Association failures likely occurred but are invisible until a successful HOT telemetry flush.

## NEXT

- Re-run with longer orchestrator timeout after verifying `aethernetio` RSSI/range.
- Consider emitting association-failure telemetry before sendto for 1 s sleep studies.
- DTIM/PS experiments deferred per plan.
