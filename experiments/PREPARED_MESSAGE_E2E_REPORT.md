# Prepared message E2E (ESP32-C6, no sleep)

Hardware: ESP32-C6, ESP-IDF v6.0.2, COM7  
Aether: `exp/esp32c6-wifi-lifecycle-diag` @ `157aadbec8e7b852d0f89274307ff7cb8103e5f7`  
Firmware build: `build-esp32c6-save-bench-smoke` (`AE_EXP_PREPARED_MESSAGE_E2E=1`)  
Receiver: `temperature_receiver/build-prepared-e2e` (`prepared_message_bench_rx_v1`)  
Session: `experiments/prepared_message_rx_session`

## Coordinated run (authoritative)

| Item | Value |
|------|-------|
| Bench client | `prepared_message_bench_v6` |
| Sender UID | `bb28fcdf-3872-4214-adb1-9ee63f05f85e` |
| Receiver UID | `fd4e309f-1619-4cbb-ada7-c69134901490` |

### Metrics (ESP serial)

```
REGISTRATION
  time_ms=1048
FULL CYCLE
  time_ms=11715
PREPARED
  raw=[2250, 2250, 2190, 2340, 2390, 2240, 2290, 2340, 2230]
  min=2190
  median=2250
  p90=2340
  max=2390
  completed=9/10
prepared block:
  reserved=10
  remaining=1
```

PREPARED #1: `wifi-failed` / `connect_timeout` (~15090 ms, nonce not consumed).  
PREPARED #2–#10: `sent` (~2.2–2.4 s each).

### Receiver (desktop console)

```
RECEIVER
  full=1/1
  prepared=9/10
  missing=1 [1]
  duplicates=0
```

Observed RECV: `FULL:0`, `PREPARED:2` … `PREPARED:10` (no `PREPARED:1`).

## Notes

- Single boot: registration → destroy Æther → FULL + block(10) → destroy Æther → 10 hot-path sends (1 s gap outside timer).
- `prepared_packet/` identical between experiment SHA and main `07841f3c`; no cherry-pick.
- No-sleep bench calls `ReleaseFullAetherWifiForHotPath()` before prepared loop (Aether leaves ESP-IDF Wi-Fi up; production uses deep-sleep reboot).
- First prepared send after release hits hot-path Wi-Fi `connect_timeout`; local RTC Wi-Fi cache not warm until after first successful connect.
- `prepared_message_e2e.tsv` — raw timings for this run.
