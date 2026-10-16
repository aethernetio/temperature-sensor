# Restore proven Æther HOT — handoff (in progress)

## GIT

| Field | Value |
|--------|--------|
| reference_branch | `feat/prepared-power-factor-study` |
| reference_sha | `fb89f439e60a6abf29b8ba86f143432e996925d8` |
| working_branch | `diag/restore-proven-aether-hot-e2e` |
| working_tree | `../temperature-sensor-working-hot` |
| final_sha | *(uncommitted local commits pending)* |
| pushed | no |

## LIBRARY

| Field | Value |
|--------|--------|
| sha | `157aadbec8e7b852d0f89274307ff7cb8103e5f7` |

## REFERENCE E2E (historical receiver)

| Field | Value |
|--------|--------|
| build | PASS (ESP32-C6, IDF 6.0.2, `build-esp32c6-save-bench-smoke`) |
| runner | `experiments/run_prepared_message_e2e_worktree.py` |
| FULL_RX | 0/1 (latest run) |
| HOT_RX | 0/10 |
| heap_panic | no (after erase-flash + PreConstructCleanup skip) |
| historical_receiver | `prepared_message_receiver` (MSVC build) |
| result | **FAIL** (registration completes; full cycle never delivers) |

Latest receiver UID (example): `57e31788-1891-44fc-98e9-3f2219f874b0`  
Latest sender UID (example): `96b74122-aca8-461f-90d9-19dc00c53e52`

## Environment fixes applied (worktree only)

1. **IDF export**: dot-source `export.ps1`, clear `PYTHONPATH` (was building for xtensa/esp32).
2. **RISC-V GCC 15 / SmallFunction**: `ae_esp_strip_aether_c_api(aether)` in `main/CMakeLists.txt`.
3. **Missing headers at pin 157aadbe**: `main/shims/aether/wifi/wifi_probe_state.h`, `main/wifi_lifecycle_out.h`.
4. **Stale SPIFFS / wrong client**: `erase-flash` before each E2E flash.
5. **Same-boot 2× AetherApp**: skip `PreConstructCleanup()` when `AE_EXP_PREPARED_MESSAGE_E2E=1` (avoids netif duplicate / load fault on stale state).

## CURRENT blocker (reference E2E)

After registration (`REGISTRATION time_ms≈22s`), **full cycle** starts but:

- Only **one** `Wifi connected` in the whole run (during registration).
- After `ReleaseApp()` → `FULL_CYCLE_START`, driver stays `NOT CONNECTED`.
- Cloud paths log `Transport build timeout` / `SERVER_CHANNEL_BUILD_FAILED` on TCP `:9020` (protocol 0); UDP `:9021` channels select but P2P write never completes within capture window.
- No `PREPARED_E2E_DONE`, receiver never prints `full=` / `prepared=`.

**Suspected regression vs historical run:** second `AetherApp` in the same boot on **ESP-IDF 6.0.2 + riscv gcc 15.2** does not bring Wi-Fi back after first app `Exit()`, even with PreConstructCleanup disabled. Historical report assumed register → destroy Æther → full cycle on one boot; may have used an older IDF/toolchain or different Wi-Fi driver behavior.

**Not yet tried (minimal next steps):**

- Single `AetherApp` for register + full (no `ReleaseApp` between phases; only `Exit` after full write).
- Or explicit `esp_wifi_stop` / full deinit path between phases (opposite of current skip).
- Compare with `prepared_power_factor_bench` deep-sleep reboot path (different experiment profile).

## NEW TCP RECEIVER E2E

Not started (blocked on reference E2E PASS).

## COMPARISON

See `experiments/RESTORE_FULL_HOT_COMPARISON.md`.

| Topic | Broken diag path (`esp_ordinary_full_ready_control`) | Restore historical path |
|--------|------------------------------------------------------|-------------------------|
| Channel rebuild / `channels.clear()` | yes | no |
| FS_INIT reliability client | yes | distillation + registration |
| Second AetherApp same boot | varies | yes (E2E bench) |

**confirmed_root_cause:** not confirmed for full-cycle stall; **wifi reconnect after first AetherApp teardown** is the leading hypothesis from serial logs.

## ARTIFACTS

| Artifact | Path |
|----------|------|
| ESP serial (latest) | `experiments/prepared_message_e2e_esp_restore.log` |
| Receiver log | `experiments/prepared_message_e2e_receiver_restore.log` |
| Result JSON | `experiments/prepared_message_e2e_restore_result.json` |
| Comparison | `experiments/RESTORE_FULL_HOT_COMPARISON.md` |

## FLAGS

| Flag | Value |
|------|--------|
| SERVER_CHANGED | no |
| AETHER_CRYPTO_CHANGED | no |
| ORIGINAL_BENCHMARK_BRANCH_CHANGED | no |
| PPK_ENERGY_MEASURED | no |
| 100X_RUN | no |

## TESTED_NOT_ASSUMED

| Check | Status |
|--------|--------|
| old FULL works | no |
| old HOT works | no |
| new receiver FULL works | not tested |
| new receiver HOT works | not tested |
