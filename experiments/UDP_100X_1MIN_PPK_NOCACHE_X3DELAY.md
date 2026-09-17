# UDP 100x / 1 MIN / PPK NO-CACHE + 3x DELAYS

- branch: `diag/udp-100x-1min-ppk-nocache-x3delay`
- sha: `24998cb517573856ca80e5e3fb36023e2c5855fc` (pre-commit build; see git HEAD after commit)
- destination: `192.168.68.84:9000`
- wifi_cache: 0 (channel/BSSID/static IP/ARP all off)
- pre_settle_ms: 150 (3x baseline 50)
- post_send_hold_ms: 600 (3x baseline 200)
- UDP attempts=100 unique=10 missing_n=90 dups={}
- note: No Wi-Fi RTC cache + 3x settle/hold. UDP delivery collapsed vs cached baseline (10/100). Stats are over non-glued RX windows only; charge gate widened to 600 mC for cold-association cost.

## SEND TIME (ms)
- valid_count=4 mean=5614.934999999966 median=4933.369999999951 p90=7797.299999999951 p95=8389.64999999996 min=3610.99999999999 max=8981.99999999997 stddev=2331.079590425858

## SEND CHARGE (mC)
- mean=442.0003379947931 median=450.1597562536168 p90=507.796710917093 p95=509.99472861785 min=355.4890931533317 max=512.192746318607 stddev=75.35242367890115

## SEND ENERGY @ 3.0V (mJ)
- mean=1326.0010139843791 median=1350.4792687608503 p90=1523.3901327512788 p95=1529.98418585355 min=1066.467279459995 max=1536.5782389558208

- interval: {'requested_s': 60.0, 'actual_start_to_start_mean_s': 491.8453113502926, 'timer_sleep_s': 60}
- PPK_RUN=PASS BUILD=PASS FLASH=PASS
- csv: `C:\Users\nickc\Projects\temperature-sensor-prepared\experiments\power_factor_results\udp_100x_1min_ppk_nocache_x3_sends.csv`

## vs cached baseline (prior)
- cached baseline: RX 74/100, median send ~728 ms / 63.9 mC / 191.6 mJ
- this no-cache x3delay: RX 10/100, median send much longer / ~5–8x charge
