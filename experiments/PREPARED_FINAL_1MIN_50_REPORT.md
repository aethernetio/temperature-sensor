# Prepared final 1-minute 50-HOT energy test

Run id: `20260903_145912`

One FULL Æther prepare + 50 HOT sends at ~60 s cadence per AP.
PPK integral spans FULL startup through HOT #50 final deep sleep.

Config: SKIP_VALIDATE=yes CPU=80 FULL_TEARDOWN=yes DISC_PM=ON WIFI_PS=NONE ENCODE_DURING_ASSOCIATION=yes (late socket) PRE chirkov=25 ms / aethernetio=0 ms POST=0 TX-done TaskNotify.

| AP | FULL | HOT attempts | RX unique | loss | elapsed | total energy J | total charge mAh | avg current mA | amortized J/message | CR2 life |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| chirkov | yes | 50 | 49 | 2.0% | 3181 s | 18.234629 | 1.688392 | 1.911003 | 0.364692572 | 17 d / 0.6 mo |
| aethernetio | yes | 50 | 46 | 8.0% | 3191 s | 30.675243 | 2.840300 | 3.204287 | 0.613504850 | 10 d / 0.3 mo |

Amortized J/message = total run energy / 50 (includes 1/50 FULL cost and sleep).

Energy is primary; RX unique/loss are factual side results (expected=50 HOT).
