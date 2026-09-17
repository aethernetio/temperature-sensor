# THERMOMETER2 WIFI CONNECT STAGE 100x

- branch: `diag/thermometer2-wifi-connect-stage-100x`
- sha: `5704ae57a034f2a84a9b018b39df62ce8df7d1cb`
- FAILURE_STAGE: **mixed**

Interpretation from counts:
- Association mostly succeeds (STA_CONNECTED 88/100).
- Dominant outcome among connected cycles is **STA_CONNECTED_BUT_NO_GOT_IP (84)** — DHCP/IP acquisition stall until 20 s timeout with no post-connect disconnect.
- Secondary: 12 cycles never reached STA_CONNECTED (handshake/auth disconnects; dominant reason 204 HANDSHAKE_TIMEOUT).

## STA_CONNECTED
- {'count': 88, 'pct': 88.0, 'fail_before_connected': 12}

## ASSOCIATION TIME
- {'n': 88, 'median_ms': 397.5, 'p90_ms': 512.3, 'p95_ms': 1201.6499999999996, 'min_ms': 310.0, 'max_ms': 1777.0}

## GOT_IP
- {'count': 4, 'pct_of_100': 4.0, 'pct_of_sta_connected': 4.545454545454546}

## DHCP TIME
- {'n': 4, 'median_ms': 1540.5, 'p90_ms': 1554.3, 'p95_ms': 1557.15, 'min_ms': 1523.0, 'max_ms': 1560.0}

## RESULT CLASS
- {'GOT_IP_SUCCESS': 4, 'STA_CONNECTED_BUT_NO_GOT_IP': 84, 'CONNECTED_THEN_DISCONNECTED_BEFORE_IP': 0, 'DISCONNECTED_BEFORE_STA_CONNECTED': 12, 'TIMEOUT_NO_TERMINAL_EVENT': 0}

## DISCONNECT REASONS (all)
- 204:HANDSHAKE_TIMEOUT: count=8 pct100=8.0% pct_disc=66.7%
- 15:4WAY_HANDSHAKE_TIMEOUT: count=2 pct100=2.0% pct_disc=16.7%
- 2:AUTH_EXPIRE: count=1 pct100=1.0% pct_disc=8.3%
- 4:ASSOC_EXPIRE: count=1 pct100=1.0% pct_disc=8.3%

## DISCONNECT before STA_CONNECTED
- [{'reason': '204:HANDSHAKE_TIMEOUT', 'code': 204, 'count': 8, 'pct_of_100': 8.0, 'pct_of_disconnects': 66.66666666666667}, {'reason': '15:4WAY_HANDSHAKE_TIMEOUT', 'code': 15, 'count': 2, 'pct_of_100': 2.0, 'pct_of_disconnects': 16.666666666666668}, {'reason': '2:AUTH_EXPIRE', 'code': 2, 'count': 1, 'pct_of_100': 1.0, 'pct_of_disconnects': 8.333333333333334}, {'reason': '4:ASSOC_EXPIRE', 'code': 4, 'count': 1, 'pct_of_100': 1.0, 'pct_of_disconnects': 8.333333333333334}]

## DISCONNECT after STA_CONNECTED before GOT_IP
- []

- csv: `C:\Users\nickc\Projects\temperature-sensor-prepared\experiments\power_factor_results\wifi_connect_stage_100x.csv`
