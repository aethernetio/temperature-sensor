# A + B integration status

## Branch

- `integration/aether-hot-udp-b-wifi` from `49ecd6815a176f219aeae3bb0f75b6f0997b57f9`
- Source A control reference: run `20261019T175349Z` (FULL 1/1, HOT 5/5), firmware `0d17da7e…`
- Source B: `8f7b8d5293c132b6512318170286eaaff9f96a84`, `main/udp_rtc_index_loop.cpp`

## Control A reproduction (vanilla HOT, before B wire)

| run_id | outcome | note |
|--------|---------|------|
| 20261019T202152Z | FULL 1/1, HOT 2/5 | runner @ 49ecd68, `HOT_CYCLE_TIMEOUT_S=52` |
| 20261019T203350Z | PPK COM3 denied | no flash |

Runner on branch: `HOT_CYCLE_TIMEOUT_S=120`, HOT phase gating, receiver tail after delivery — **no** `post_bootstrap_no_rx` abort.

## Integration boundary

- `main/udp_cached_lwip_send.{h,cpp}` — cached Wi-Fi + raw lwIP UDP (from B), arbitrary payload length
- `EncodePreparedPayload` / `PreparedEndpointIpv4` — Æther encode + prepared endpoint
- `simple_full_hot_101_test.cpp` — HOT via `SendOnce` when `AE_SIMPLE_HOT_UDP_LWIP_B=1` (CMake default for this target)

## Next hardware steps

1. Free COM3 (close nRF Connect serial), rerun `SIMPLE_FULL_HOT_COUNT=5` smoke
2. Combined smoke: expect `HOT_WIFI=b_lwip_cached` in serial, MSG_NUM 1…6 from sender UID
3. If smoke OK: one run `SIMPLE_FULL_HOT_COUNT=100`, finalize JSON (no RUNNING)
