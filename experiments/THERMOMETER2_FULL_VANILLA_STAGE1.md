# Thermometer 2 — Stage 1 (full Æther + vanilla Wi-Fi)

Branch: `diag/thermometer2-aether-full-vanilla`  
Parent snapshot: `2a3befdc2c384c9eec2fceeab5005ad9c76aefa1` (from `diag/restore-proven-aether-hot-e2e`)

## Isolated target

| Item | Path |
|------|------|
| Firmware source | `main/thermometer2_aether_full_vanilla_bench.cpp` |
| CMake flag | `AE_EXP_THERMOMETER2_FULL_VANILLA=1` |
| Build dir | `build-thermometer2-aether-full-vanilla` |
| Runner | `experiments/run_thermometer2_aether_full_vanilla.py` |
| sdkconfig | `sdkconfig.defaults.thermometer2_aether_full_vanilla` (+ `sdkconfig.defaults.bench` for smoke UART) |
| Receiver | `temperature_receiver` @ prepared repo → `aether_hot_tcp_receiver --require-tcp` |
| Æther pin | `157aadbec8e7b852d0f89274307ff7cb8103e5f7` via `deps/aether-client-cpp-157aadbe-diag` |

## Vanilla Wi-Fi (effective when built with runner)

- `AE_EXP_WIFI_CANONICAL=1` (disables default AMPDU-off / scan-threshold / retry10 / fail-disconnect bundle in `ae_exp_wifi.h`)
- No `AE_EXP_WIFI_FEAT_*` set
- `WiFiPowerSaveParam.wifi_ps_type = WIFI_PS_NONE`
- No prepared/HOT/cache bench flags
- Driver handoff: `ReleaseOwnedDriverForPhaseHandoff()` before sleep (`AE_EXP_THERMOMETER2_FULL_VANILLA`)

## Cycle model

- Timer wake → one `AetherApp::Construct` → `SelectClient` → `P2pStream` + one `Write` → 1 s post-success pump → `Save` → driver release → `BoardPrepareDeepSleep` → **20 s** deep sleep
- Cold power-on: **60 s** USB window (no repeat after timer wake)
- Active timeout: **60 s** (`AE_THERM_FULL_ACTIVE_TIMEOUT_MS`)
- Message ID: `run_id = (session << 16) | cycle` in `ReliabilityPayload` (type `kFull`)

## Status (2026-10-16, updated)

- [x] Branch + isolated target/runner/build dir
- [x] Board sleep powerdown + GPIO cast fix for IDF 6 / `-Werror`
- [x] Controlled Æther copy + driver-release guard for `AE_EXP_THERMOMETER2_FULL_VANILLA`
- [x] **Build** (smoke config): `CMAKE_BUILD_PARALLEL_LEVEL=1` — firmware SHA256 `925de6c5…db3794`
- [x] Runner: `--smoke`, `--measure` (PPK @ 3.0 V), journal parse, segmenter `max_wake_s=65`
- [x] Firmware: per-cycle `JOURNAL` lines + `JOURNAL_DUMP` after 100 cycles; STA/GOT_IP weak hooks
- [ ] Functional smoke 3×20 s + receiver RX 3/3 (**blocked: no COM VID_303A**)
- [ ] 100× PPK + CSV/JSON/FULL_VANILLA report (**blocked: same**)

## Next commands

```powershell
# Build smoke (3 cycles) or measure (100 cycles) firmware:
python experiments/run_thermometer2_aether_full_vanilla.py --build-only --smoke

# Smoke (Espressif COM + desktop receiver):
python experiments/run_thermometer2_aether_full_vanilla.py --smoke

# 100-cycle PPK + artifacts (PPK2 + COM + receiver):
python experiments/run_thermometer2_aether_full_vanilla.py --measure
```

PREPARED_USED=no · HOT_USED=no · WIFI_CACHE_USED=no · OPTIMIZED_UDP_PROFILE_CHANGED=no
