# Æther HOT 100x / 2s / PPK / TCP receiver

## Campaign status (fresh; do not mix with pre-fix artifacts)

| Gate | State |
|------|--------|
| SMOKE (5 HOT + PPK) | **FAIL** — see `experiments/power_factor_results/aether_hot_smoke_fixed.json` |
| FULL_CAMPAIGN (100 + RUN2/3) | **NOT_RUN** |
| Energy (HOT mC / sleep) | **NOT_CONFIRMED** (`energy_confirmed: false`) |

**Do not** treat `aether_hot_smoke_5_ppk_tcp_rx.json` (~9000 ms windows, ~453 mC, 0/5 RX) as the fixed-firmware smoke. That run predates the current segmenter and FULL/HOT ordering fixes.

## Latest smoke artifact (fixed code path)

- Branch: `diag/aether-hot-e2e-fix-100x-2s`
- Git SHA (runner): `e7e1016b8bdba4c075d1365d0333ddb390e7f1fb`
- Firmware bin SHA256: `eed7e0e8b5f0f8be9593fe5bd611032204800a85374cec70d7162220f331c8ee`
- Receiver UID: `5aade50f-00d9-4624-b097-e203cdcf1e38`
- TCP receiver: up (`tcp_verified: true`)
- `AETHER_FULL_READY` on receiver: **no**
- HOT RX: **0/5**
- PPK: `clipped_windows=5`, `valid_windows=0` (force-closed wakes; not measured HOT energy)
- Serial markers (`FULL_READY_WRITE_SUCCESS`, `PREPARED_EXPORT_SUCCESS`): **not seen** (COM5 opens; console rebuild applied)

## Code fixes landed (pushed)

1. P2P `P2pPortHandle{}` for FULL (not `CreatePort`)
2. `reliability_full_v1` + `FS_INIT=experiments/preprovision/sender_fs_157aadbe.h`
3. PPK `ESP_RST_POWERON` → fresh `kFullPrepare` RTC
4. Smoke runner: firmware hash, FULL-before-HOT wait, serial retry, optional erase
5. USB console for 5-HOT smoke (`AE_EXP_BISECT_CONSOLE` + sdkconfig patch)

## Blockers before PASS / 100× campaign

1. **Chip erase** over COM5 fails intermittently (PPK/USB); stale NVS may prevent `SelectClient(reliability_full_v1)` / FS load. Manual `idf.py -p COM5 erase-flash` once, then re-run smoke.
2. **FULL path** still not observed on receiver; needs UART proof after erase (expect `FULL_READY_WRITE_SUCCESS` before `PREPARED_EXPORT_SUCCESS`).

## Commands

```text
py -3.11 experiments/run_aether_hot_100x_2s_ppk_tcp_rx.py --smoke-only --skip-receiver-build --skip-erase
py -3.11 experiments/run_aether_hot_100x_2s_ppk_tcp_rx.py --skip-receiver-build
```

Primary JSON: `experiments/power_factor_results/aether_hot_smoke_fixed.json`
