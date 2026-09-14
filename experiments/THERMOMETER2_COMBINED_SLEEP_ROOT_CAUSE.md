# Thermometer 2 — combined sleep root cause

- Branch: `diag/thermometer2-combined-sleep-root-cause`
- SHA: `9e0e977ccb20f71500ef071b606a3cead00ba215`
- Voltage: 3000 mV
- Sleep metric: **raw contiguous average** (no sample-current filter)
- Q/T cross-check required

## Measurement bug (prior report)

- Prior combined sleep ≈8.5 µA was **invalid** (`ua < 200` filtering).
- Manual PPK contiguous window: **8.41 mA** = 343.16 mC / 40.79 s.
- Marked SUPERSEDED in `THERMOMETER2_LOW_ENERGY_WIFI_PLUS_SLEEP.md`.

| variant | operation before sleep | raw avg µA | charge mC | duration s | wake cause | verdict |
|---|---|---:|---:|---:|---|---|
| B1_CORRECTED_CONTROL | sleep-only B1_LED_ON_LOW | 7099.3046910114035 | 237.62718374264313 | 34.862772941589355 | timer_expected | FAIL |
| COMBINED_P0_BROKEN | UDP+teardown apply_min_pd=0 | 9156.985453777905 | 321.0376357652571 | 34.92331051826477 | timer_after_udp | FAIL |
| COMBINED_P1_MIN_PD | UDP+teardown apply_min_pd=1 | 9193.954855987884 | 306.2949836624849 | 34.923728466033936 | timer_after_udp | FAIL |
| B1_SPIKEFILTER_FIXED_GPIO | B1+PPK spikefilter25 | 7620.57004924922 | 256.10816676957893 | 34.91790723800659 | timer | FAIL |
| Z_FULL_QUIET_SPIKEFILTER | Z_FULL+PPK spikefilter25 | 7133.417657785643 | 254.68447960805847 | 34.92132759094238 | timer | FAIL |
| COMBINED_P1_SPIKEFILTER | UDP+minPD+spikefilter25 | 7474.8795445477035 | 240.82476460355915 | 34.922523498535156 | timer_after_udp | FAIL |
| B1_AUTORANGE_DESPIKE | B1+autorange_despike | 3.6219980611188265 | 0.10862397539281789 | 29.990070000000003 | timer | TARGET |
| COMBINED_P1_AUTORANGE_DESPIKE | UDP+minPD+autorange_despike | 3.8601677664050604 | 0.11578646558519541 | 29.99519 | timer_after_udp | TARGET |
| CONFIRM1_AUTORANGE_DESPIKE | UDP+minPD+despike | 3.858394515567382 | 0.09647217116768922 | 25.00319 | timer_after_udp | TARGET |
| CONFIRM2_AUTORANGE_DESPIKE | UDP+minPD+despike | 3.84798197585462 | 0.0961921227911521 | 24.99807 | timer_after_udp | TARGET |
| CONFIRM3_AUTORANGE_DESPIKE | UDP+minPD+despike | 3.852279065856205 | 0.09629954174780801 | 24.99807 | timer_after_udp | TARGET |

## Notes

- PPK_ZERO_AVG_UA=0.110
- P1 did not reach <=25 µA; stop before confirm — continue bisect

## Final 10x60

```json
{
  "wakes_detected": 41,
  "measured_wakes": 10,
  "active_mC": [
    11.986160920394694,
    10.75911613618982,
    6.141001111504844,
    9.395669512870183,
    1.106698403635126,
    2.4315124147848053,
    26.55423012222387,
    6.089275630424839,
    4.063464174635652,
    22.9707869492022
  ],
  "active_mean_mC": 10.149791537586603,
  "active_median_mC": 7.768335312187514,
  "sleep_windows": [
    {
      "t0_s": 33.838499999999996,
      "t1_s": 50.4905,
      "duration_s": 16.624860000000005,
      "n_samples": 967,
      "charge_C": 8.057521210734232e-05,
      "charge_mC": 0.08057521210734232,
      "avg_uA": 4.846670113753878,
      "avg_from_charge_uA": 4.846670113753878,
      "median_uA": 2.978,
      "p95_uA": 8.045799999999996,
      "max_uA": 49.99,
      "min_uA": -0.911,
      "q_over_t_rel_err": 0.0,
      "q_over_t_ok": true,
      "sample_filter_used": false,
      "ua_lt200_filter_used": false,
      "autorange_despike": {
        "thr_uA": 50.0,
        "max_burst_s": 0.001,
        "fill_uA": 2.978,
        "removed_runs": 108,
        "removed_samples": 243,
        "removed_frac": 0.2512926577042399,
        "method": "ppk_autorange_despike",
        "applied": true,
        "fs_hz": 100000.0
      },
      "raw_mean_before_despike_uA": 6421.574531540848
    },
    {
      "t0_s": 72.561,
      "t1_s": 342.32047,
      "duration_s": 269.73233,
      "n_samples": 15674,
      "charge_C": 0.001167873925808893,
      "charge_mC": 1.1678739258088928,
      "avg_uA": 4.329751371698354,
      "avg_from_charge_uA": 4.329751371698354,
      "median_uA": 2.978,
      "p95_uA": 7.882,
      "max_uA": 49.99,
      "min_uA": -1.043,
      "q_over_t_rel_err": 0.0,
      "q_over_t_ok": true,
      "sample_filter_used": false,
      "ua_lt200_filter_used": false,
      "autorange_despike": {
        "thr_uA": 50.0,
        "max_burst_s": 0.001,
        "fill_uA": 2.978,
        "removed_runs": 1671,
        "removed_samples": 4262,
        "removed_frac": 0.27191527370167157,
        "method": "ppk_autorange_despike",
        "applied": true,
        "fs_hz": 100000.0
      },
      "raw_mean_before_despike_uA": 10273.806410297308
    }
  ],
  "sleep_window_avg_uA": 4.588210742726115,
  "sleep_window_worst_uA": 4.846670113753878,
  "total_charge_C": 3.7930025080685192,
  "csv": "C:\\Users\\nickc\\Projects\\temperature-sensor-prepared\\experiments\\power_modes_raw\\thermometer2_combined_sleep_root_cause\\final10_capture.csv",
  "measure_meta": {
    "samples": 33933008,
    "avg_uA": 1034.126411063155,
    "max_uA": 1289626.727561276
  }
}
```
