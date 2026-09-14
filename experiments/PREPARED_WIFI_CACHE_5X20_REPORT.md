# Prepared Wi-Fi cache 5×20 (ESP32-C6, no sleep, silent)

Hardware: ESP32-C6, ESP-IDF v6.0.2, COM7  
Aether: `exp/esp32c6-wifi-lifecycle-diag` @ `157aadbec8e7b852d0f89274307ff7cb8103e5f7` (unchanged)  
Firmware: `AE_EXP_PREPARED_WIFI_CACHE_5X20=1`, `AE_EXP_SILENT=1`, console/log NONE  
Client: `prepared_wifi_cache_5x20_v1`  
Receiver UID: `5aade50f-00d9-4624-b097-e203cdcf1e38`  
Post-send hold: **300 ms included in every prepared timing**

## REGISTRATION

```
time_us=13962906
```

(~14.0 s; first Construct + network registration + Save + full release)

## FULL

```
raw=[5637828, 4124003, 4014012, 4104022, 4184009]
min=4014012
median=4124003
max=5637828
n=5
```

(~4.0–5.6 s; Construct → Select → FULL write → PrepareSendMessageBlock(20) → Save → release)

## FIRST PREPARED

Only **1/5** first-of-cycle timings were recovered (timing of prepared #1 is carried in prepared #2; most #2 UDP deliveries were lost).

```
raw=[15100453]
median=15100453
n=1
```

(~15.1 s) — cold Wi-Fi after Aether release, before local cache population.

## WARM PREPARED

```
n=46
min=610471
median=640476
p90=690482
p99=730476
max=730476
```

(~0.61–0.73 s including 300 ms hold ⇒ ~0.31–0.43 s net)

## ALL PREPARED (recovered timings)

```
n=47
min=610471
median=640476
p90=690531
p99=15100453
max=15100453
```

## DELIVERY

```
full=5/5
prepared=44/100
final=1/1
missing=56
duplicates=0
out_of_order=0
```

Fire-and-forget prepared UDP path; losses not retried. Application sequence gaps match missing prepared deliveries.

## CACHE

```
BSSID reuse confirmed=yes (hits=46)
channel reuse=yes (same flag path)
static IP reuse confirmed=yes (hits=46)
DHCP skipped confirmed=yes (hits=46)
fallbacks=0
```

`cache_flags=7` = `UsedBssid | UsedStaticIp | DhcpSkipped`.

## Notes

- `CleanupHotPathWifiRuntime()` no longer clears `address_is_valid` / `bs_is_valid`.
- Fast path waits on `WIFI_EVENT_STA_CONNECTED` when static IP is cached; GOT_IP only on cold/DHCP path.
- Cached BSSID failure invalidates cache and falls back once (not observed this run).
- Silent build: no UART results; all timings via binary Æther payloads.
- Warm prepared ~640 ms vs prior no-cache ~2250 ms — local prepared Wi-Fi cache is effective.
