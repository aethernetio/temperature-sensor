# UDP 100x / 1 MIN / PPK BASELINE

- branch: diag/udp-100x-1min-ppk-current
- sha: 8214e2a21cabc8f1029ebff447849a48112ff291
- destination: 192.168.68.84:9000
- cold_boot_awake_wait_s: 60 (timer wake skips)
- firmware_behavior_changed_except_boot_wait: no
- UDP attempts=100 unique=74 missing=[6, 11, 13, 15, 17, 20, 24, 26, 32, 35, 36, 37, 38, 40, 46, 49, 50, 52, 54, 63, 71, 73, 84, 88, 93, 97] dups={}
- PPK valid_count=70 (PPK_RUN=PASS)

## SEND TIME (ms)
- mean=1245.362000000001 median=728.1700000000092 p90=2191.000000000713 p95=2832.349999999961 min=529.0000000004511 max=7600.000000000364 stddev=1091.8676395769971

## SEND CHARGE (mC)
- mean=108.06071376907613 median=63.85766321328279 p90=213.99193466351028 p95=219.68496342580417 min=9.268579522267007 max=427.03117774824665 stddev=83.55488437694775

## SEND ENERGY @ 3.0V (mJ)
- mean=324.1821413072284 median=191.57298963984834 p90=641.9758039905308 p95=659.0548902774125 min=27.805738566801022 max=1281.09353324474

- first10: {'mean_time_ms': 1087.3711111111052, 'median_time_ms': 726.34, 'mean_charge_mC': 103.96227877951198, 'median_charge_mC': 63.847063606099205, 'n': 9}
- last10: {'mean_time_ms': 1355.8571428571408, 'median_time_ms': 916.0000000001673, 'mean_charge_mC': 117.0077706387051, 'median_charge_mC': 63.37510586633958, 'n': 7}
- interval: {'requested_s': 60.0, 'timer_sleep_s': 60, 'actual_start_to_start_mean_s': 59.61058553994871, 'actual_start_to_start_median_s': 59.720362424850464, 'adjacent_pairs_n': 51, 'note': 'start-to-start computed only for adjacent received sequence pairs (excludes gaps from UDP loss)', 'mean_including_loss_gaps_s': 80.93305295134243}
- filtering_of_active_samples=no despike=no sleep_energy=no
- csv: experiments/power_factor_results/udp_100x_1min_ppk_sends.csv
- windows: experiments/power_factor_results/udp_100x_1min_ppk_windows/final (not committed)

## Notes
- Firmware path unchanged except cold-boot 60s awake wait on non-TIMER wake and measured count=100.
- UDP RX unique=74/100; missing packets are delivery losses (not ACK/retries).
- PPK valid=70: 26 missing_ppk_window (no RX to arm segment) + 4 charge_out_of_range (glued active windows).
- Send charge is bimodal: typical hot ~50-90 mC vs slower association ~180-250 mC; median tracks hot path.
- Sleep energy not included; no active-sample filter; no despike.
