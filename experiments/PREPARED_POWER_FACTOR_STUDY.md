# ESP32-C6 HOT Prepared-Send Power Factor Study

Hardware campaign results will be appended after PPK runs complete.

## Infrastructure (implementation commit)

- Fixed P4 hot path per AP (chirkov PRE=25, aethernetio PRE=0); no adaptive search in bench firmware.
- TX-done: Task Notification (no `vTaskDelay` poll loop).
- Silent measured path: `AE_EXP_SILENT`, no application UART/USB console.
- External RTC crystal required in sdkconfig (`CONFIG_RTC_CLK_SRC_EXT_CRYS=y`).
- PPK arm via `BENCH_ARM` over TCP probe_receiver (not serial).
- 100 HOT attempts, 2000 ms timer deep sleep between attempts.

## Æther crypto (preserved)

Call graph for prepared HOT `EncodePacket` (`aether/prepared_packet/packet_encoder.cpp`):

1. `PreparedSendMessageKeyProvider` + `next_nonce.Next()` (nonce consume)
2. `SyncEncryptProvider` — encryption/authentication backend
3. `LoginApi` → `login_by_alias` → `AuthorizedApi::send_message(AeMessage{...})` — signing/auth path inside API stack
4. `ProtocolContext::Pack()` — wire packet

| Field | Value |
|---|---|
| HOT_SIGN_OPERATION | `AuthorizedApi::send_message` (via LoginApi alias auth) |
| HOT_ENCRYPT_OPERATION | `SyncEncryptProvider` |
| crypto backend | `SyncEncryptProvider` / `PreparedSendMessageKeyProvider` |
| AE_SIGNATURE in build | preserved through API send path |
| Protocol semantics changed for bench | **no** |

## PHY partial after deep sleep

To be confirmed against ESP-IDF 6.0.2 during B13 / Phase C runs.

## Results

_Pending hardware campaign._
