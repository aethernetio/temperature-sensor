# Prepared Power Best Config

run_id: `20260902_142719`

Follow-on to `PREPARED_POWER_FACTOR_CONFIRMATION.md` (c2709d5).
Goal: STOP Wi-Fi (`esp_wifi_stop`) without deinit, on top of confirmed
`SKIP_VALIDATE + CPU80`, portable across chirkov and aethernetio.

## Teardown semantics

| Policy | Behavior |
|---|---|
| FULL | unregister handlers, stop, deinit, destroy netif, delete event loop |
| STOP_FULL_SAFE | unregister handlers, `esp_wifi_stop`, no deinit |
| STOP_MINIMAL | `esp_wifi_stop` only |
| STOP_DISCONNECT | `esp_wifi_disconnect` (+≤150 ms wait), unregister, stop |
| DIRECT_DEEP_SLEEP | historical only — no stop; AP-dependent; forbidden for portable |

## Main table

See `experiments/power_factor_results/best_config.tsv`.

- **A_SKIP_CPU80_STOP** (chirkov): status=OK RX=98/100 teardown=STOP_MINIMAL CPU=80 E_mean=136.82639625449042 wake_med=363.7065000000064 pathology=False
- **B_SKIP_CPU160_STOP** (chirkov): status=OK RX=98/100 teardown=STOP_MINIMAL CPU=160 E_mean=129.1797412958393 wake_med=358.50000000000296 pathology=False
- **C_NOSKIP_CPU80_STOP** (chirkov): status=OK RX=100/100 teardown=STOP_MINIMAL CPU=80 E_mean=215.30702747299853 wake_med=660.3869999999929 pathology=False
- **D_SKIP_CPU80_FULL** (chirkov): status=OK RX=99/100 teardown=FULL CPU=80 E_mean=118.48959125018656 wake_med=369.04399999998463 pathology=False
- **E_SKIP_CPU160_FULL** (chirkov): status=OK RX=98/100 teardown=FULL CPU=160 E_mean=131.83416516473295 wake_med=363.14400000000546 pathology=False
- **F_NOSKIP_CPU80_FULL** (chirkov): status=NINJA_FAILED RX=None/None teardown=None CPU=None E_mean=None wake_med=None pathology=None
- **LONG1000_AETHERNETIO** (aethernetio): status=OK RX=973/1000 teardown=FULL CPU=80 E_mean=143.5768851432632 wake_med=435.8634999999822 pathology=False
- **LONG1000_CHIRKOV** (chirkov): status=OK RX=961/1000 teardown=FULL CPU=80 E_mean=198.31482653860232 wake_med=572.5270000000364 pathology=False
- **P0_FULL_AETHERNETIO** (aethernetio): status=OK RX=93/100 teardown=FULL CPU=80 E_mean=129.20770958720706 wake_med=459.5084999999841 pathology=False
- **P0_FULL_CHIRKOV** (chirkov): status=OK RX=99/100 teardown=FULL CPU=80 E_mean=103.89947219858439 wake_med=362.25650000000087 pathology=False
- **P1_STOP_SAFE_AETHERNETIO** (aethernetio): status=OK RX=100/100 teardown=STOP_FULL_SAFE CPU=80 E_mean=129.2124906061271 wake_med=420.6015000000036 pathology=False
- **P1_STOP_SAFE_CHIRKOV** (chirkov): status=OK RX=99/100 teardown=STOP_FULL_SAFE CPU=80 E_mean=116.89314587760371 wake_med=358.24950000001013 pathology=False
- **P2_STOP_MIN_AETHERNETIO** (aethernetio): status=OK RX=95/100 teardown=STOP_MINIMAL CPU=80 E_mean=141.43117058312157 wake_med=429.734499999995 pathology=False
- **P2_STOP_MIN_CHIRKOV** (chirkov): status=OK RX=98/100 teardown=STOP_MINIMAL CPU=80 E_mean=164.59021131479176 wake_med=363.40149999999835 pathology=False
- **SLEEP60_AETHERNETIO** (aethernetio): status=OK RX=27/30 teardown=FULL CPU=80 E_mean=None wake_med=None pathology=False
- **SLEEP60_CHIRKOV** (chirkov): status=OK RX=27/30 teardown=FULL CPU=80 E_mean=None wake_med=None pathology=False

## PORTABLE WINNER

SKIP_VALIDATE=True
CPU=80
teardown=FULL
disconnected PM=ON
connected PS=NONE
encode during association=False
PRE chirkov=25 ms
PRE aethernetio=0 ms
POST=0
external RTC=yes
console/logging=NONE

### CHIRKOV
attempts=100 RX unique=99 loss=1.0000000000000009
E mean/median/p90=103.89947219858439/97.07859488638425/119.29681869887249
wake mean/median/p90=379.4345408163253/362.25650000000087/414.6783999999968

### AETHERNETIO
attempts=100 RX unique=93 loss=6.999999999999995
E mean/median/p90=129.20770958720706/127.59312636509767/151.35299640122847
wake mean/median/p90=470.1808061224499/459.5084999999841/529.3031000000049

NEXT ASSOCIATION CONFLICT: observed=no
FULL TEARDOWN NEEDED: yes
esp_wifi_deinit NEEDED BEFORE DEEP SLEEP: yes

### Battery (CR2 800 mAh @ 3.0 V, sleep 8 µA, MEAN energy)
1 min: I_avg=0.585 mA life≈57.0 d
10 min: I_avg=0.066 mA life≈507.2 d

## ChatGPT handoff

```
PREPARED LOW-POWER FINAL
PORTABLE_WINNER=FULL
SDKCONFIG=SKIP_VALIDATE_IN_DEEP_SLEEP + EXT_CRYS + DISC_PM_ON + CONSOLE_NONE
CPU=80
TEARDOWN=FULL
DEINIT_BEFORE_SLEEP=yes
DISCONNECTED_PM=ON
CONNECTED_PS=NONE
ENCODE_OVERLAP=False
LONG1000=chirkov 961/1000, aethernetio 973/1000, pass=true
SLEEP60=chirkov 27/30, aethernetio 27/30, pass=true
PRODUCTION_DEFAULTS=AETHER_PREPARED_HOT_TEARDOWN_POLICY=0, AETHER_PREPARED_HOT_CPU_MHZ=80
SERVER_CHANGED=no
ALL_USEFUL_CHANGES_PUSHED=pending
```

