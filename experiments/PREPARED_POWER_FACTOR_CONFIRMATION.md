# Prepared Power Factor Confirmation (in progress)

run started after discovering **build contamination** in the 35-variant campaign.

## Contamination (confirmed before hardware re-measure)

- Shared build dir: `build-esp32c6-pf-fresh`
- `force_sdk_measured()` **appended** `CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP=y` for B1 (variant 10) and **never cleared** it for later variants.
- That explains nearly identical wake times ~353–354 ms for B1/B2/B3/B7 vs A0 ~441 ms.
- `PowerBenchOptions.disconnected_pm` was **never applied** to Wi-Fi or sdkconfig; apparent B2 win was contaminated SKIP_VALIDATE, not DISC_PM_OFF.
- Fix: set/clear sticky Kconfig each configure; confirmation uses isolated `build-power-confirm/<run>/<cfm>/<ap>/`.

## IO_TEARDOWN exact semantics

Variant **206 IO_TEARDOWN** = `TeardownPolicy::kDirectDeepSleep` (**same as B12**).

After successful TX-done, `CleanupHotPathWifiRuntime(2)`:

- returns immediately
- does **not** unregister handlers
- does **not** call `esp_wifi_stop`
- does **not** call `esp_wifi_deinit`
- does **not** destroy netif / event group

Deep sleep is armed by the bench afterward. Naming “IO_TEARDOWN” is misleading — it is the **skip-cleanup / direct deep-sleep** path.

## Status

Hardware confirmation runner: `experiments/run_power_factor_confirmation.py`  
Config audit: `experiments/audit_power_factor_config.py`  

Results will land in:

- `experiments/PREPARED_POWER_FACTOR_CONFIRMATION.md` (this file, updated after runs)
- `experiments/power_factor_results/confirmation_energy.tsv`
- `experiments/power_factor_confirmation/<run_id>/`
