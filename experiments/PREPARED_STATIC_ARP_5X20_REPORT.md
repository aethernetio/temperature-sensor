# Prepared static ARP 5×20 (ESP32-C6, no sleep, silent)

Hardware: ESP32-C6, ESP-IDF v6.0.2, COM7  
Aether: `exp/esp32c6-wifi-lifecycle-diag` @ `157aadbec8e7b852d0f89274307ff7cb8103e5f7` (**unchanged**)  
Firmware: `AE_EXP_PREPARED_WIFI_CACHE_5X20=1`, silent console/log NONE  
Client: `prepared_wifi_cache_5x20_v1`  
Receiver UID: `5aade50f-00d9-4624-b097-e203cdcf1e38`  
Post-send hold: **300 ms** (included in prepared timings)

## Changes vs `669ccdc3…`

- Local prepared Wi-Fi cache extended with `gateway_mac[6]` + `gateway_mac_valid`
- Cold/DHCP path resolves gateway MAC via lwIP ARP (`etharp_request` / `etharp_find_addr` on tcpip thread)
- `CapturePreparedWifiCacheFromActiveConnection()` exports BSSID/channel/IP/gw/MAC from live FULL Æther Wi-Fi before release
- Fast path installs static ARP (`etharp_add_static_entry` via `esp_netif_tcpip_exec`) before `EncodePacket` / `sendto`
- Payload flags: `used_static_arp`, `arp_fallback`, `wifi_fallback` (plus existing BSSID/IP/DHCP bits)

## Comparison to previous (`669ccdc3…`)

| Metric | BEFORE (`669ccdc`) | AFTER (static ARP + FULL cache export) |
|---|---|---|
| prepared delivery | **44/100** | **29/100** |
| warm median | ~640 ms | ~650 ms |
| FIRST PREPARED | ~15.1 s cold (1/5 recovered) | **~660–760 ms** (3/5 recovered) |
| used_static_arp | n/a | **32 hits** |
| arp_fallback | n/a | 0 |
| wifi_fallback | 0 | 0 |

**Verdict:** FULL→prepared cache export fixed FIRST PREPARED (no longer ~15 s cold). Static ARP installed and used (`flags=15`). Delivery did **not** improve (still far from 100/100); slightly worse in this run.

## REGISTRATION

```
time_us=251137
```

(~0.25 s) — NVS already had registration after earlier erase+boot; not a cold cloud register.

## FULL

```
raw=[4738679, 3471545, 2911524, 3591534, 2871515]
min=2871515
median=3471545
max=4738679
n=5
```

## FIRST PREPARED (after FULL cache export)

```
raw=[760476, 730476, 660476]
min=660476
median=730476
max=760476
n=3
```

(~0.66–0.76 s including 300 ms hold) — **fast**, matches warm path.

### FIRST PREPARED BEFORE vs AFTER

| | BEFORE | AFTER |
|---|---|---|
| FIRST PREPARED | ~cold / ~15 s | ~730 ms median |

## WARM PREPARED

```
n=29
min=610482
median=650477
p90=720478
p99=3020485
max=3020485
```

(~0.65 s median including 300 ms hold). Two ~3.0 s outliers.

## ALL PREPARED (recovered timings)

```
n=32
min=610482
median=650482
p90=730476
p99=3020485
max=3020485
```

## DELIVERY

```
full=6/5
prepared=29/100
final=1/1
missing=71
duplicates=1
out_of_order=0
```

`full=6/5` / `duplicates=1`: one duplicated FULL outer=1 after mid-run hard reset.

## CACHE

```
BSSID reuse confirmed=yes (hits=32)
static IP reuse confirmed=yes (hits=32)
DHCP skipped confirmed=yes (hits=32)
used_static_arp hits=32
arp_fallback hits=0
wifi_fallback hits=0
```

`cache_flags=15` = `UsedBssid | UsedStaticIp | DhcpSkipped | UsedStaticArp`.

## Control: hold=600 ms × 20 prepared

Same fast path + static ARP; only hold increased to 600 ms; one outer × 20.

```
prepared delivery = 6/20  (~30%)
timings ~930–970 ms (includes 600 ms hold)
used_static_arp on recovered samples
```

**600 ms did not improve delivery** (same ~30% rate as 29/100). Unlikely that post-`sendto` async TX flush alone explains losses under a 300 ms hold.

## Notes / next hypotheses

- Static ARP + cache export are working; reliability bottleneck is elsewhere (Wi-Fi teardown vs UDP path, AP/router drop, packet size/path, or association instability — see ~3 s outliers).
- Do not increase hold further without a new hypothesis.
- Prefer targeted diagnostics (e.g. keep Wi-Fi up across prepared burst, or confirm UDP arrives at gateway) over broad refactors.
