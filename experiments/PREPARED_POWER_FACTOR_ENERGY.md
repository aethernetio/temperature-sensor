# Prepared Power Factor — Energy Report

PPK2 @ **3000 mV**. Spike-tolerant sleep plateaus (I ≤ 1000 µA for 1.2–3.5 s, spikes ≤ 80 ms ignored). Cycle energy = ∫I·V over the **wake** window between consecutive sleep plateaus. Decimation stride=10. First/last cycles trimmed.

- CSVs OK: **34** / 34
- Missing capture: `1_chirkov`
- Baseline for ratios: `0_chirkov` median **118.614 mJ**/cycle (wake 441.4 ms)
- TSV: `experiments/power_factor_results/energy_report.tsv`
- JSON: `experiments/power_factor_results/energy_report.json`

## Ranking (lowest median wake energy)

| # | key | variant | n | E_med mJ | E_mean mJ | vs base | wake_ms |
|---:|---|---|---:|---:|---:|---:|---:|
| 1 | 206_aethernetio | IO_TEARDOWN | 98 | 82.308 | 99.579 | 0.694 | 322.0 |
| 2 | 10_chirkov | B1_SKIP_VALIDATE_DEEP_SLEEP | 98 | 98.511 | 99.195 | 0.831 | 353.5 |
| 3 | 12_chirkov | B3_WIFI_PS_MIN | 98 | 99.153 | 118.258 | 0.836 | 353.3 |
| 4 | 11_chirkov | B2_DISC_PM_OFF | 98 | 100.854 | 108.342 | 0.850 | 353.3 |
| 5 | 16_chirkov | B7_CPU80 | 98 | 101.274 | 107.935 | 0.854 | 353.8 |
| 6 | 15_chirkov | B6_CPU120 | 98 | 101.299 | 149.270 | 0.854 | 353.8 |
| 7 | 22_chirkov | B13_PHY_PARTIAL_EVERY_WAKE | 98 | 101.598 | 105.627 | 0.857 | 353.2 |
| 8 | 14_chirkov | B5_WIFI_PS_MAX_LI3 | 98 | 102.113 | 111.540 | 0.861 | 353.7 |
| 9 | 150_chirkov | C11_PHY_FINAL | 98 | 102.360 | 107.245 | 0.863 | 353.8 |
| 10 | 101_chirkov | C2_CPU_MAX_LI1 | 98 | 102.790 | 119.285 | 0.867 | 360.4 |
| 11 | 18_chirkov | B9_PMF_OFF | 98 | 103.083 | 259.256 | 0.869 | 357.1 |
| 12 | 100_chirkov | C1_CPU_MIN | 98 | 103.632 | 122.460 | 0.874 | 362.9 |
| 13 | 111_chirkov | C4_ALS_MIN | 98 | 103.891 | 107.422 | 0.876 | 362.8 |
| 14 | 112_chirkov | C5_ALS_MAX_LI1 | 98 | 104.002 | 110.638 | 0.877 | 358.0 |
| 15 | 20_chirkov | B11_WIFI_STOP_ONLY | 98 | 104.089 | 110.995 | 0.878 | 353.8 |
| 16 | 110_chirkov | C3_ALS_NONE | 98 | 104.266 | 114.479 | 0.879 | 363.2 |
| 17 | 120_chirkov | C6_PRE_MIN_TO_NONE | 98 | 104.285 | 113.130 | 0.879 | 362.0 |
| 18 | 13_chirkov | B4_WIFI_PS_MAX_LI1 | 98 | 104.834 | 197.814 | 0.884 | 363.2 |
| 19 | 17_chirkov | B8_CPU40 | 98 | 105.718 | 158.426 | 0.891 | 354.3 |
| 20 | 131_chirkov | C9_PMF_OFF_CPU80 | 98 | 105.866 | 118.671 | 0.893 | 363.7 |
| 21 | 130_chirkov | C8_ENCODE_CPU80 | 98 | 106.228 | 122.250 | 0.896 | 353.7 |
| 22 | 121_chirkov | C7_PRE_MAX_TO_NONE | 98 | 106.658 | 125.096 | 0.899 | 362.6 |
| 23 | 19_chirkov | B10_ENCODE_DURING_ASSOCIATION | 98 | 106.828 | 124.251 | 0.901 | 362.3 |
| 24 | 201_aethernetio | IO_DISC_PM_OFF | 98 | 116.916 | 176.184 | 0.986 | 409.9 |
| 25 | 0_chirkov | A0_CLEAN | 98 | 118.614 | 127.482 | 1.000 | 441.4 |
| 26 | 207_aethernetio | IO_PMF_OFF | 98 | 119.781 | 129.903 | 1.010 | 434.2 |
| 27 | 200_aethernetio | IO_A0 | 98 | 122.319 | 214.125 | 1.031 | 430.4 |
| 28 | 208_aethernetio | IO_PHY | 98 | 122.896 | 120.908 | 1.036 | 429.9 |
| 29 | 205_aethernetio | IO_BEST_OVERALL | 98 | 124.988 | 134.542 | 1.054 | 414.4 |
| 30 | 203_aethernetio | IO_BEST_PS | 98 | 125.374 | 192.934 | 1.057 | 442.7 |
| 31 | 204_aethernetio | IO_BEST_DFS | 98 | 126.724 | 202.629 | 1.068 | 451.5 |
| 32 | 202_aethernetio | IO_BEST_CPU | 98 | 126.739 | 178.468 | 1.068 | 449.9 |
| 33 | 21_chirkov | B12_DIRECT_DEEP_SLEEP | 98 | 458.793 | 453.355 | 3.868 | 1478.8 |
| 34 | 140_chirkov | C10_TEARDOWN_MATRIX | 98 | 464.068 | 463.496 | 3.912 | 1485.5 |

## All variants

| key | variant | n | E_med mJ | E_mean mJ | p90 mJ | vs base | wake_ms | RX |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 0_chirkov | A0_CLEAN | 98 | 118.614 | 127.482 | 138.219 | 1.000 | 441.4 | 100 |
| 10_chirkov | B1_SKIP_VALIDATE_DEEP_SLEEP | 98 | 98.511 | 99.195 | 107.919 | 0.831 | 353.5 | 96 |
| 11_chirkov | B2_DISC_PM_OFF | 98 | 100.854 | 108.342 | 120.060 | 0.850 | 353.3 | 99 |
| 12_chirkov | B3_WIFI_PS_MIN | 98 | 99.153 | 118.258 | 122.263 | 0.836 | 353.3 | 93 |
| 13_chirkov | B4_WIFI_PS_MAX_LI1 | 98 | 104.834 | 197.814 | 145.418 | 0.884 | 363.2 | 100 |
| 14_chirkov | B5_WIFI_PS_MAX_LI3 | 98 | 102.113 | 111.540 | 115.254 | 0.861 | 353.7 | 91 |
| 15_chirkov | B6_CPU120 | 98 | 101.299 | 149.270 | 116.713 | 0.854 | 353.8 | 92 |
| 16_chirkov | B7_CPU80 | 98 | 101.274 | 107.935 | 113.744 | 0.854 | 353.8 | 96 |
| 17_chirkov | B8_CPU40 | 98 | 105.718 | 158.426 | 121.719 | 0.891 | 354.3 | 99 |
| 18_chirkov | B9_PMF_OFF | 98 | 103.083 | 259.256 | 140.053 | 0.869 | 357.1 | 99 |
| 19_chirkov | B10_ENCODE_DURING_ASSOCIATION | 98 | 106.828 | 124.251 | 132.160 | 0.901 | 362.3 | 99 |
| 20_chirkov | B11_WIFI_STOP_ONLY | 98 | 104.089 | 110.995 | 120.929 | 0.878 | 353.8 | 100 |
| 21_chirkov | B12_DIRECT_DEEP_SLEEP | 98 | 458.793 | 453.355 | 472.653 | 3.868 | 1478.8 | 100 |
| 22_chirkov | B13_PHY_PARTIAL_EVERY_WAKE | 98 | 101.598 | 105.627 | 116.148 | 0.857 | 353.2 | 99 |
| 100_chirkov | C1_CPU_MIN | 98 | 103.632 | 122.460 | 128.346 | 0.874 | 362.9 | 96 |
| 101_chirkov | C2_CPU_MAX_LI1 | 98 | 102.790 | 119.285 | 130.617 | 0.867 | 360.4 | 94 |
| 110_chirkov | C3_ALS_NONE | 98 | 104.266 | 114.479 | 125.931 | 0.879 | 363.2 | 99 |
| 111_chirkov | C4_ALS_MIN | 98 | 103.891 | 107.422 | 122.494 | 0.876 | 362.8 | 100 |
| 112_chirkov | C5_ALS_MAX_LI1 | 98 | 104.002 | 110.638 | 121.275 | 0.877 | 358.0 | 98 |
| 120_chirkov | C6_PRE_MIN_TO_NONE | 98 | 104.285 | 113.130 | 128.584 | 0.879 | 362.0 | 100 |
| 121_chirkov | C7_PRE_MAX_TO_NONE | 98 | 106.658 | 125.096 | 128.069 | 0.899 | 362.6 | 98 |
| 130_chirkov | C8_ENCODE_CPU80 | 98 | 106.228 | 122.250 | 125.328 | 0.896 | 353.7 | 94 |
| 131_chirkov | C9_PMF_OFF_CPU80 | 98 | 105.866 | 118.671 | 129.982 | 0.893 | 363.7 | 98 |
| 140_chirkov | C10_TEARDOWN_MATRIX | 98 | 464.068 | 463.496 | 485.536 | 3.912 | 1485.5 | 78 |
| 150_chirkov | C11_PHY_FINAL | 98 | 102.360 | 107.245 | 115.151 | 0.863 | 353.8 | 99 |
| 200_aethernetio | IO_A0 | 98 | 122.319 | 214.125 | 878.139 | 1.031 | 430.4 | 93 |
| 201_aethernetio | IO_DISC_PM_OFF | 98 | 116.916 | 176.184 | 140.651 | 0.986 | 409.9 | 99 |
| 202_aethernetio | IO_BEST_CPU | 98 | 126.739 | 178.468 | 138.373 | 1.068 | 449.9 | 95 |
| 203_aethernetio | IO_BEST_PS | 98 | 125.374 | 192.934 | 154.504 | 1.057 | 442.7 | 98 |
| 204_aethernetio | IO_BEST_DFS | 98 | 126.724 | 202.629 | 378.068 | 1.068 | 451.5 | 100 |
| 205_aethernetio | IO_BEST_OVERALL | 98 | 124.988 | 134.542 | 145.917 | 1.054 | 414.4 | 98 |
| 206_aethernetio | IO_TEARDOWN | 98 | 82.308 | 99.579 | 98.084 | 0.694 | 322.0 | 100 |
| 207_aethernetio | IO_PMF_OFF | 98 | 119.781 | 129.903 | 132.215 | 1.010 | 434.2 | 100 |
| 208_aethernetio | IO_PHY | 98 | 122.896 | 120.908 | 135.597 | 1.036 | 429.9 | 100 |

## Interpretation

- **E_med / E_mean**: median/mean energy of one HOT wake (prepare+TX+teardown), not including deep-sleep idle.
- **vs base**: ratio of median cycle energy to baseline A0 (or first OK chirkov if A0 failed).
- Absolute mJ depends on decimation; use **ratios and ranking** for comparing factors.
- Variants with fewer cycles still usable if n≥70; check RX unique column for packet delivery.

