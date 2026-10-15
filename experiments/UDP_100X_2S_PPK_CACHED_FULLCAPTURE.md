# UDP 100x / 2 S / PPK CACHED FULL CAPTURE

Previous **15/100** PPK result from UDP-gated segmentation is **superseded**.

- Segmentation is **independent of UDP RX** (streaming PPK envelope + offline re-segment).
- Start-to-start interval is computed from **PPK wake times**, not receiver timestamps.
- Firmware unchanged: `AE_UDP_WIFI_CACHE=1`, `AE_UDP_PERIOD_S=2`, `AE_UDP_MEASURED_SENDS=100`.

## Result (final run)

| | |
|---|---|
| PPK measured windows | **100/100** (valid **100/100**) |
| PPK_RUN | **PASS** |
| UDP RX (PPK run) | **100/100** |
| Delivery A / B | **100/100**, **99/100** (B missing seq 78) |
| Aggregate delivery | **299/300** |

## Energy @ 3.0 V (valid measured windows, n=100)

| | TIME (ms) | CHARGE (mC) | ENERGY (mJ) |
|---|---:|---:|---:|
| median | 760 | 53.65 | 160.94 |
| mean | 987 | 76.71 | 230.14 |
| p95 | 2772 | 249.45 | 748.36 |

## Interval (PPK)

- start-to-start median: **2.0 s** (mean 2.23 s, p95 4.0 s)
- sleep between wakes median: **~1.24 s**

## Artifacts

- `experiments/power_factor_results/udp_100x_2s_ppk_fullcapture.json`
- `experiments/power_factor_results/udp_100x_2s_ppk_fullcapture_sends.csv`
- Raw capture (local, not in git): `experiments/power_factor_results/udp_100x_2s_ppk_fullcapture_raw/`

## Runner

`experiments/run_udp_100x_2s_ppk_fullcapture.py`
