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

Hardware campaign finished **2026-09-02** on ESP32-C6 + PPK2.

| Metric | Value |
|---|---|
| Variants | **35/35 OK** (0 failures) |
| HOT params | 100 attempts, 2000 ms deep sleep |
| RX unique range | 78–100 (only `140_chirkov` &lt; 90) |
| PPK capture missing | `1_chirkov` (early campaign; discarded oversized CSV) |
| Raw CSV | 34 files under `experiments/power_modes_raw/` (~17.5 GB) |
| Checkpoint | `experiments/power_factor_checkpoint.json` |
| Per-variant table | `experiments/power_factor_results_summary.md` |

### Energy (PPK @ 3000 mV)

Spike-tolerant wake-energy analysis of 34/34 CSVs (missing `1_chirkov`):

- Full report: [`PREPARED_POWER_FACTOR_ENERGY.md`](PREPARED_POWER_FACTOR_ENERGY.md)
- TSV: `power_factor_results/energy_report.tsv`

Lowest median wake energy: **`206_aethernetio` (IO_TEARDOWN) ~82 mJ**.  
Highest outliers: **`140_chirkov` / `21_chirkov` ~460 mJ** (long wake ~1.5 s).

### Known analysis gaps

- Campaign-time analyzer (`analyze_power_factor_ppk.py`) failed on spike-fragmented sleep; batch report uses hysteresis/spike-tolerant plateaus + decimation.
- Runner mid-campaign fixes (RTC power-off before flash, `NO_ARM` fail-fast, HOT idle-done) are in `run_prepared_power_factor_study.py` and should ship with the results commit.

### Notes

- `140_chirkov` (C10_TEARDOWN_MATRIX): unique=78 despite max_seq=100 (higher loss).
- Stale-RTC / wrong-variant HOT without `BENCH_ARM` occurred twice; recovered via power-cycle + `NO_ARM` retry.
