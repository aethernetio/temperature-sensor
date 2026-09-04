# Prepared FULL vs HOT — chirkov 1-minute

Run id: 20260903_200343
Starting SHA: 8c255a45aca25fdb5600fed6d751bd3870ccf406

Same cadence (~60 s start-to-start). Sleep kept in both averages.
HOT total = combined run − prep FULL energy.

| mode | attempts | RX | loss | total energy J | avg energy/msg J | avg current mA |
|---|---:|---:|---:|---:|---:|---:|
| FULL | 10 | 10 | 0.0% | 19.307298 | 1.930730 | 9.823168 |
| HOT | 10 | 8 | 20.0% | 3.011576 | 0.301158 | 0.167310 |

HOT_PREP_FULL_ENERGY_J=7.719917803
HOT_RUN_TOTAL_ENERGY_J=10.731493386
HOT_10_TOTAL_ENERGY_J=3.011575583

SAVING_J=1.629572
HOT_VS_FULL=0.1560
SAVING_PERCENT=84.4
APPROX_HOT_ACTIVE_MINUS_SLEEP_J=0.299718
(sleep@8 uA ~1.44 mJ/min)

CR2_HOT_LIFE=199.2 d / 6.55 mo (from HOT avg current 0.1673 mA)
FULL_REFRESH_EXTRA_PER_100_HOT_J=0.019307

Old ~100-120 mJ HOT benchmarks compare to APPROX_HOT_ACTIVE_MINUS_SLEEP (order-of-magnitude sanity only).
