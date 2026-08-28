# Prepared KEEP-WIFI-UP 5×20 (ESP32-C6, silent)

Hardware: ESP32-C6, ESP-IDF v6.0.2, COM7  
Aether: `exp/esp32c6-wifi-lifecycle-diag` @ `157aadbec8e7b852d0f89274307ff7cb8103e5f7` (**unchanged**)  
Firmware: `AE_EXP_PREPARED_KEEP_WIFI_UP_5X20=1`, silent console/log NONE  
Client: `prepared_keep_wifi_up_5x20_v1`  
Receiver UID: `5aade50f-00d9-4624-b097-e203cdcf1e38`  
Block reserve: **20** (unchanged prepared packet path)

## Hypothesis

Losses on prepared sends may be caused by **full Wi-Fi teardown + reassociation after every prepared message**.

## A/B change (bench-only)

Production `SendPreparedOnce()` unchanged.

New session API:

- `BeginPreparedWifiSession()` — init, associate, static IP/ARP once; leave Wi-Fi up
- `SendPreparedPacketOnActiveWifi()` — EncodePacket → socket → sendto → close (no stop/deinit/reconnect)
- `EndPreparedWifiSession()` — stop/deinit/cleanup after message #20

Per outer cycle: FULL → PrepareSendMessageBlock(20) → capture Wi-Fi cache → release FULL Æther → Begin session → 20 prepared sends (1 s gap, **no** post-send hold) → End session.

Socket is still create/close **per message** (extra socket A/B not required).

## previous

| Experiment | Delivery |
|---|---|
| static ARP + teardown each send | **29/100** |
| hold 300→600 ms (control) | **6/20 ≈ 30%** |

## new

| Experiment | Delivery |
|---|---|
| keep Wi-Fi up (5×20) | **96/100** |

### DELIVERY detail

```
full=6/5
prepared=96/100
final=1/1
missing=4
duplicates=1
out_of_order=0
```

Cache flags on recovered prepared: BSSID/IP/DHCP-skip/static-ARP hits=99 (payload flag path active).

## wifi_session_start

Measured once per outer cycle (init → association → static IP/ARP ready). Carried on PREPARED#1 as `previous_full_us`. Only 2/5 first-of-burst messages recovered, so n=2:

```
raw=[254249, 279549]
min=254249
median=279549
max=279549
n=2
```

(~0.25–0.28 s)

## prepared send (encode + socket + sendto + close only)

No 300 ms post-send hold in this mode.

### FIRST PREPARED (send-only)

```
raw=[2467, 2415, 2418, 3785, 2413]
min=2413
median=2418
max=3785
n=5
```

### ALL PREPARED (send-only)

```
n=99
min=2180
median=2600
p90=2710
p99=3785
max=3785
```

(~2.6 ms median)

## Verdict

```
LOSS LOCATION = WIFI RECONNECT/TEARDOWN
```

KEEP-WIFI-UP reaches **96/100** (criterion ~95–100/100). Packet encoding / server / nonce / UDP path is adequate when Wi-Fi stays associated; prior ~30% delivery tracks full teardown/reassociation between each prepared send.

Do **not** chase further Wi-Fi micro-optimizations (ARP, hold) for this loss class — next product work is avoiding per-message Wi-Fi lifecycle, not changing prepared packets.
