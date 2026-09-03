# Prepared final 1-minute 100-HOT energy test

Run id: `20260903_092646`

One FULL Æther prepare + 100 HOT sends at ~60 s cadence per AP.
PPK integral spans FULL startup through HOT #100 final deep sleep.

ENCODE_OVERLAP_IMPLEMENTED=yes (Wi-Fi association starts before EncodePacket).

| AP | FULL | HOT attempts | RX unique | loss | elapsed | total energy J | total charge mAh | avg current mA | amortized J/message | CR2 life |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| chirkov | yes | 100 | 0 | 100.0% | 7641 s | 9.826122 | 0.909826 | 0.428667 | 0.098261220 | 78 d / 2.5 mo |
| aethernetio | yes | 100 | 0 | 100.0% | 6232 s | 9.579065 | 0.886950 | 0.512327 | 0.095790648 | 65 d / 2.1 mo |

Amortized J/message = total run energy / 100 (includes 1/100 FULL cost and sleep).

ENCODE_OVERLAP_PRODUCTION_READY: no

Both APs delivered FULL+ARM successfully but RX unique=0 / loss=100% over the
100 HOT @ 60 s window with `encode_during_association=yes`. Energy numbers are
valid for the measured cadence; end-to-end delivery failed on both APs, so
`encode_during_association` is not production-ready as a default. Production
defaults were not changed by this test.
