# VANILLA WIFI 100x / 20s SLEEP

- branch: `diag/thermometer2-udp-vanilla-100x-20s`
- sha: `bb9a6991bb765b3b98bcc8ab5f889307cb78c263`
- destination: `192.168.68.84:9000`

## CONNECTION
- got_ip_success=20 fail=80 pct=20.0%
- got_ip_time_median_ms=None p95=None

## UDP
- send_called=20 send_ok=20 local_fail=0
- rx_unique=20 dups={}
- network_lost_after_send_ok=0 loss_pct=0.0
- end_to_end_success_pct=20.0

## ENERGY ALL
- n=100 median_mC=2204.3953039044936 mean_mC=2038.7632826798576 p95_mC=2238.5281691645855 median_ms=35000.009999999864 p95_ms=35000.0100000002

- ENERGY_GOT_IP_SUCCESS: {'n': 20, 'median_mC': 1765.1668004633843, 'mean_mC': 1804.1164725148467, 'p95_mC': 2142.2174309675356, 'median_ms': 35000.00999999997, 'mean_ms': 35000.00799999997, 'p95_ms': 35000.0100000002}
- ENERGY_GOT_IP_FAILURE: {'n': 80, 'median_mC': 2214.2780915427793, 'mean_mC': 2097.42498522111, 'p95_mC': 2238.789479095431, 'median_ms': 35000.00999999975, 'mean_ms': 35000.00774999993, 'p95_ms': 35000.0100000002}
- ENERGY_SEND_OK_RX: {'n': 20, 'median_mC': 1765.1668004633843, 'mean_mC': 1804.1164725148467, 'p95_mC': 2142.2174309675356, 'median_ms': 35000.00999999997, 'mean_ms': 35000.00799999997, 'p95_ms': 35000.0100000002}
- ENERGY_SEND_OK_LOST: {'n': 0, 'median_mC': None, 'mean_mC': None, 'p95_mC': None, 'median_ms': None, 'mean_ms': None, 'p95_ms': None}

## CONFIG
- cache=NONE DHCP=yes ARP=normal_lwip WIFI_PS=NONE
- fixed_rate=no internal_retry_override=no
- settle_after_GOT_IP=100ms post_send_hold=1000ms deep_sleep=20s

- csv: `C:\Users\nickc\Projects\temperature-sensor-prepared\experiments\power_factor_results\udp_vanilla_100x_20s_cycles.csv`

## NOTES
- Diagnostic dump not received; SEND_OK/network_loss inferred from RX only when dump missing.
- Primary failure: cold GOT_IP 20/100 (CONNECTION), not proven datagram loss after send.
- PPK many FORCE_CLOSE@35s — energy/duration are upper bounds.
- End-to-end 20%%; target was >=95%% e2e and <=2%% loss after SEND_OK (loss inconclusive without dump).
