# Thermometer 2 — combined sleep root cause

- Branch: `diag/thermometer2-combined-sleep-root-cause`
- SHA: `45ceeb7be1eaa1ecbe162efefaf0a0a74e05ec01`
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

## Notes

- PPK_ZERO_AVG_UA=0.110
- P1 did not reach <=25 µA; stop before confirm — continue bisect

## Final 10x60

```json
{
  "wakes_detected": 83,
  "measured_wakes": 10,
  "active_mC": [
    8.597250396960046,
    17.02306942235997,
    60.29978685743978,
    15.399531833759818,
    11.719362401879907,
    14.459948960159833,
    8.872352302079886,
    3.8030051686799906,
    6.403170139320081,
    4.879004113800045
  ],
  "active_mean_mC": 15.145648159643935,
  "active_median_mC": 10.295857351979897,
  "sleep_windows": [
    {
      "t0_s": 40.34,
      "t1_s": 338.85112,
      "duration_s": 298.48967999999996,
      "n_samples": 17518,
      "charge_C": 2.73742941673817,
      "charge_mC": 2737.42941673817,
      "avg_uA": 9170.56293606576,
      "avg_from_charge_uA": 9170.934877005362,
      "median_uA": 4.082,
      "p95_uA": 51121.24314999999,
      "max_uA": 596877.056,
      "min_uA": -1.043,
      "q_over_t_rel_err": 4.055813609205384e-05,
      "q_over_t_ok": true,
      "sample_filter_used": false
    }
  ],
  "sleep_window_avg_uA": 9170.56293606576,
  "sleep_window_worst_uA": 9170.56293606576,
  "total_charge_C": 3.2750436862957706,
  "csv": "C:\\Users\\nickc\\Projects\\temperature-sensor-prepared\\experiments\\power_modes_raw\\thermometer2_combined_sleep_root_cause\\final10_capture.csv",
  "measure_meta": {
    "samples": 33585888,
    "avg_uA": 9029.103628084526,
    "max_uA": 676531.1046170652
  }
}
```
