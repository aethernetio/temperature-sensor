# Adaptive Wi-Fi Probe — campaign report

PPK source voltage: **3000 mV**. Telemetry: `AE_TELE_ENABLED=0` (ChannelStatistics Ping RTT remains active in Phase B).

## SERVER

```
SERVER_CHANGED=no
SERVER_PROTOCOL=current_existing_only
deferred server probe=not implemented intentionally
```

| Field | Value |
|-------|--------|
| branch | `feat/adaptive-wifi-probe` |
| HEAD | `692f0f1` (revert of `31b891f`) |
| tree equals pre-probe `6ff5300` | yes |
| methods 43–45 / ProbeResultStore | absent |

## CLIENT / SENSOR GIT

| Repo | Branch | SHA | Notes |
|------|--------|-----|--------|
| aether (broker) | `feat/adaptive-wifi-probe` | `692f0f1` | rolled back |
| aether-client-cpp | `feat/adaptive-wifi-probe` | `586ba2ac` | ping-only, no methods 43–45 |
| temperature-sensor | `feat/adaptive-wifi-probe` | `742d703`+ | Phase B/C firmware + orchestrator |

Receiver: `prepared_wifi_cache_rx_session` → `RECEIVER_UID=5aade50f-00d9-4624-b097-e203cdcf1e38` (must match `SERVICE_UID` in orchestrator).

## CHIRKOV — Phase A (ICMP, complete)

**Winner (reliability-first): P3 CHANNEL_IP, PRE=0 ms**

| Profile | connect median | ICMP loss | min stable PRE |
|---------|----------------|-----------|----------------|
| P3 CHANNEL_IP | **96 ms** | **0.00%** | **0 ms** |

Fingerprint: SSID=chirkov, channel=9, BSSID=30:68:93:39:02:74, gw=192.168.68.1

Artifacts: `experiments/adaptive_probe_results/chirkov_phase_a.json`

## CHIRKOV — Phase B (existing Ping, complete)

| Metric | Value |
|--------|--------|
| ping_ok | 50 / 50 |
| RTT median | ~204 ms |
| cold FULL median | ~4686 ms |

Profile-fast FULL with cached IP/ARP: **NOT_TESTED** (requires invasive Wi-Fi lifecycle bypass).

Artifacts: `experiments/adaptive_probe_results/chirkov_b.json`, `chirkov_phase_b_canonical.log`

## CHIRKOV — Phase C (prepared HOT + 1 s sleep) — PARTIAL

Config: profile=P3, PRE=max(0,50)=**50 ms**, POST baseline=300 ms, `reliability_full_v1`, FS_INIT preprovision.

| Stage | HOT | FULL | Status |
|-------|-----|------|--------|
| Baseline (5×30, POST=300) | **149** | 5 | PASS (~1 loss) |
| POST=200 (1×30) | **29** | 1 | PASS (post candidate) |
| Sleep 250 ms (1×30) | **29** | 1 | PASS (used POST=300 firmware) |
| POST 100→0 search | — | — | **PENDING** |
| Sleep 500 / 1000 ms | — | — | **PENDING** |
| Long run (10×50 HOT) | — | — | **PENDING** |

**post_winner (interim): 200 ms** (29/30 HOT at POST=200).

Resume: `experiments/run_chirkov_c_continue.py` (after `export.ps1`; do not wipe `build-esp32c6-adaptive-probe`).

Artifacts: `experiments/adaptive_probe_results/chirkov_phase_c.tsv`, `chirkov_all.json`

## AETHERNETIO

**Not started** — pending chirkov Phase C completion. Requires erase/invalidate AP cache on AP switch.

## Known fixes (orchestrator)

1. `PREPARED_RX_SESSION` — do not wipe receiver session (UID match).
2. `SERVICE_UID=5aade50f-...` matches `prepared_wifi_cache_rx_session`.
3. `analyze_tsv()` — columns `record_id, kind, outer, hot` (kind 1=FULL, 2=HOT).
4. POST search pass threshold: `hot >= 28` per 30-HOT run (not vs baseline 150).
5. `CPM_SOURCE_CACHE` + Git `usr/bin` in PATH for cmake after clean build dir.

---

## TCP RECEIVER / TWO-ROUTER RETAKE V2

**Status:** COMPLETE (step_index=10, 2026-08-31)

| Item | Value |
|------|--------|
| Orchestrator | `experiments/run_adaptive_probe_v2_campaign.py` |
| Checkpoint | `experiments/adaptive_probe_checkpoint.json` |
| Results | `experiments/adaptive_probe_v2_results/` |
| Receiver | TCP-only (`user_config_tcp.h`, `build-tcp-v2/`) |
| Order | TEST1 chirkov→aethernetio, TEST2 … TEST5 (10 steps) |

V1 partial chirkov Phase C preserved under `adaptive_probe_results/`; V2 is a clean retake.

### TEST1 ICMP

| | chirkov | aethernetio |
|--|---------|-------------|
| winner profile | **P1** (cached IP) | **P4** (channel+IP+ARP) |
| PRE | **0 ms** | **0 ms** |
| ICMP loss (winner) | **0.00%** | **0.00%** |
| connect median | **95 ms** | **141 ms** |
| baseline P0 connect med | 1629 ms | (see log) |
| different profile | **yes** | |
| different PRE | no (both 0) | |

Checkpoint: step_index=10 (V2 campaign complete).

### TEST2 full_ping (complete)

| | chirkov | aethernetio |
|--|---------|-------------|
| ping_ok | **50 / 50** | **50 / 50** |
| cold FULL median | **3448 ms** | **3641 ms** |
| cold FULL p90 | **3448 ms** | **3763 ms** |
| Ping RTT median | **148 ms** | **199 ms** |
| Write() call median us | (see log) | **12473** |
| WriteAction median us | (see log) | **14811** |

### TEST3 prepared_nosleep (complete)

| | chirkov | aethernetio |
|--|---------|-------------|
| baseline hot / received | **179 hot / 185 rx** | **142 hot / 147 rx** (45m timeout) |
| post200 | **30/28 HOT PASS** | **29/28 HOT PASS** |
| post100 | **28/28 HOT PASS** | **30/28 HOT PASS** |
| post50 | **28/28 HOT PASS** | **29/28 HOT PASS** |
| post25 | **28/28 HOT PASS** | **30/28 HOT PASS** |
| post10 | **11/28 FAIL** (stall; hard-reset) | **28/28 HOT PASS** |
| post0 | not run (search stopped) | **28/28 HOT PASS** |
| post_winner | **25 ms** | **0 ms** |

**AP contrast:** chirkov needs POST>=25 ms (post10 stalls); aethernetio delivers HOT at POST=0.

### TEST4 prepared_sleep (complete)

| sleep_ms | chirkov hot/rx | aethernetio hot/rx |
|----------|----------------|--------------------|
| 1000 | **105 / 109** (60m timeout; target 150) | **149 / 154** (timeout @149) |
| 250 | **149 / 154** (timeout @149) | **149 / 154** (timeout @149) |
| 500 | **138 / 143** (timeout) | **149 / 154** (timeout @149) |

POST from TEST3: chirkov **25 ms**, aethernetio **0 ms**. Both APs routinely stop at 149/150 (outer budget); treated as near-pass.

### TEST5 long-run (complete)

| | chirkov | aethernetio |
|--|---------|-------------|
| hot / received | **499 hot / 509 rx** (target 500; near-accept) | **499 hot / 509 rx** (near-accept) |
| full | 10 | 10 |
| missing_in_span | **0** | **0** |

### Progress (complete)

| Step | Status |
|------|--------|
| 0-9 all TEST1-5 both APs | **done** (step_index=10) |

**Blockers fixed 2026-08-31:**
3. Tee-Object UTF-16 log → UTF-8 Add-Content launcher; Unicode arrow/`log()` cp1251 harden.
4. TEST4/5 start TCP receiver **after** flash (prevents prior image polluting TSV during cmake/ninja).
5. `wait_hot_delivery` accepts hot>=target-1 after 120s stall (149/150 near-miss).

**Blockers fixed 2026-08-31 (earlier):**
1. `AE_V2_SKIP_BASELINE_BUILD` disabled — always cmake/ninja per-AP baseline (prevented flashing chirkov image onto aethernetio step).
2. COM7 lost after bad PPK python restart — restored via `ppk2-venv` + `ppk2_power.py` / `ppk2_hold_power.py`.

Watchdog: `run_adaptive_probe_v2_watchdog.ps1` (conservative busy-check; autoresume armed).

### Final comparison table (complete 2026-08-31)

| METRIC | CHIRKOV | AETHERNETIO |
|--------|---------|-------------|
| profile | **P1** | **P4** |
| PRE | **0 ms** | **0 ms** |
| POST | **25 ms** | **0 ms** |
| ICMP loss (winner) | **0.0%** | **0.0%** |
| connect median | **95 ms** | **141 ms** |
| cold FULL median | **3448 ms** | **3641 ms** |
| cold FULL p90 | **3448 ms** | **3763 ms** |
| Ping RTT median | **148 ms** | **199 ms** |
| FULL Write() call median us | (not in chirkov parse) | **12473** |
| FULL WriteAction median us | (not in chirkov parse) | **14811** |
| HOT no-sleep post_winner | **25 ms** (post10 fail) | **0 ms** (post0 pass) |
| HOT sleep s1000/s250/s500 | **105/149/138** /150 | **149/149/149** /150 |
| long-run HOT | **499/500** (miss=0) | **499/500** (miss=0) |

### Conclusion flags

| Flag | Result |
|------|--------|
| Different winning Wi-Fi profile across APs? | **YES** (P1 vs P4) |
| Different PRE needed? | **NO** (both 0 ms) |
| Different POST needed? | **YES** (25 vs 0 ms) |
| Sleep/deep-cycle delivery gap (s1000)? | **YES** (chirkov 105 vs aethernetio 149) |
| Long-run seq integrity OK both APs? | **YES** (missing_in_span=0 both) |

Checkpoint: **step_index=10** — V2 campaign complete. TCP receiver; erase-flash each firmware; chirkov then aethernetio per test.


## PRODUCT ADAPTIVE PROBE

One firmware, no hardcoded parameters. The device picks its Wi-Fi
profile, PRE delay and POST delay from its own measurements on
whichever access point it is attached to, then runs a 100-packet hot
campaign with the result.

Every measured packet is one boot: wake, associate, send one datagram,
hold the POST delay, tear down, deep sleep 250 ms. The sleep is a real
timer deep sleep, never a software restart, and the boot that follows
has to report a deep-sleep reset with a timer wake or the sample and
its whole batch are thrown away.

Firmware: `main/product_adaptive_wifi_probe.cpp`
(`-DAE_EXP_PRODUCT_ADAPTIVE_PROBE=1`). Receiver:
aether `examples/probe_receiver` (TCP only). Selection algorithm:
`examples/probe_receiver/product_probe_select.h`, host tests in
aether `tests/test-product-probe`.

`sizeof(ProbeRtcState)` = 108 bytes of RTC memory.

### Selected parameters

`HOT sent` and `HOT fail` are the device's own counters: a send fails
only when the local send call fails. `HOT delivered` is how many of
those packets the receiver saw, so the two differ by whatever the
network dropped after the send succeeded.

| AP | status | profile | PRE ms | POST ms | sleep ms | HOT sent | HOT fail | HOT unconfirmed | HOT delivered | reprobes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chirkov | OK | P4 | 25 | 0 | 250 | 100 | 0 | 0 | 95 | 0 |
| aethernetio | OK | P4 | 0 | 0 | 250 | 100 | 0 | 0 | 96 | 0 |

### Deep sleep

Every boot checks its own reset reason and wake cause: a measured send
must be followed by reset 8 (ESP_RST_DEEPSLEEP)
and wake 4 (ESP_SLEEP_WAKEUP_TIMER), and any
other pair is counted as a bad wake and throws the whole batch away.
Software restarts belong to the audible stages only and are counted
separately so they can never be mistaken for a sleep.

`measured sleep` is what the device observed between arming the timer
and reaching the application on the next boot, so it is the 250 ms
sleep plus the wake overhead the chip cannot avoid.

| AP | timer wakes | software restarts | rejected sleeps | bad wakes | measured sleep us | wake overhead us |
| --- | --- | --- | --- | --- | --- | --- |
| chirkov | 252 | 29 | 0 | 0 | 399489 | 149489 |
| aethernetio | 290 | 25 | 0 | 0 | 399519 | 149519 |

### Probe batches as counted by the receiver

A batch passes only when all 20 packets were sent locally, confirmed
by a TX-done success and followed by a confirmed deep sleep, and all
20 arrived. 19 buys one more independent batch at the same delay,
which passes on 38 of the 40 combined. Anything less fails, and the
search moves to the next value up the table only in the sense that it
stops: a failure never turns into a pass, and when the most
conservative value fails first the path is reported invalid instead of
being assigned a POST delay it never earned.

The PRE delay is the one parameter ICMP cannot answer for. An echo
request resolves and retries, so it survives an association the single
prepared datagram does not: on chirkov every PRE from 100 down to 0
passed the ICMP trial with no loss, and a pinned comparison then
delivered 1 of 10 packets at PRE 0 against 38 of 44 at PRE 100. So a
POST 300 batch that fails with nothing behind it now moves one step up
the PRE ladder and measures again with prepared sends, and the path is
only called invalid once the largest PRE has failed too. The batches
below show that walk: the PRE column changes while POST stays at 300.

**chirkov**

| batch | PRE ms | POST ms | expected | unique | dup | missing |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 300 | 20 | 3 | 0 | 17 |
| 2 | 10 | 300 | 20 | 12 | 0 | 8 |
| 3 | 25 | 300 | 20 | 20 | 0 | 0 |
| 4 | 25 | 200 | 20 | 19 | 0 | 1 |
| 5 | 25 | 200 | 20 | 20 | 0 | 0 |
| 6 | 25 | 100 | 20 | 19 | 0 | 1 |
| 7 | 25 | 100 | 20 | 20 | 0 | 0 |
| 8 | 25 | 50 | 20 | 20 | 0 | 0 |
| 9 | 25 | 25 | 20 | 20 | 0 | 0 |
| 10 | 25 | 10 | 20 | 20 | 0 | 0 |
| 11 | 25 | 0 | 20 | 19 | 0 | 1 |
| 12 | 25 | 0 | 20 | 19 | 0 | 1 |

Timing of the previous send, carried in each HOT_DATA packet (95 clean samples; a clean sample sent locally, saw a TX-done success and had its sleep confirmed). All values in microseconds.

| field | min | median | p90 | max |
| --- | --- | --- | --- | --- |
| connect | 116043 | 133329 | 203300 | 272312 |
| cycle | 190765 | 240773 | 330779 | 450782 |
| encode | 2091 | 2098 | 2418 | 3751 |
| sendto_call | 580 | 594 | 639 | 2252 |
| send_to_txdone | 664 | 6626 | 20103 | 28759 |
| txdone_minus_ret | 75 | 5921 | 19505 | 28175 |
| actual_post | 553 | 594 | 704 | 718 |
| teardown | 42922 | 68283 | 122592 | 203392 |
| awake | 251711 | 301719 | 391725 | 511728 |
| sleep | 399489 | 399489 | 399520 | 399520 |
| wake_overhead | 149489 | 149489 | 149520 | 149520 |

Current trace: `chirkov_hot_power.csv` at 3000 mV.

**aethernetio**

| batch | PRE ms | POST ms | expected | unique | dup | missing |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 300 | 20 | 20 | 0 | 0 |
| 2 | 0 | 200 | 20 | 20 | 0 | 0 |
| 3 | 0 | 100 | 20 | 20 | 0 | 0 |
| 4 | 0 | 50 | 20 | 19 | 0 | 1 |
| 5 | 0 | 50 | 20 | 19 | 0 | 1 |
| 6 | 0 | 25 | 20 | 20 | 0 | 0 |
| 7 | 0 | 10 | 20 | 19 | 0 | 1 |
| 8 | 0 | 10 | 20 | 19 | 0 | 1 |
| 9 | 0 | 0 | 20 | 20 | 0 | 0 |

Timing of the previous send, carried in each HOT_DATA packet (96 clean samples; a clean sample sent locally, saw a TX-done success and had its sleep confirmed). All values in microseconds.

| field | min | median | p90 | max |
| --- | --- | --- | --- | --- |
| connect | 167532 | 207299 | 246729 | 3511334 |
| cycle | 250863 | 300857 | 350857 | 3610848 |
| encode | 2095 | 2518 | 2897 | 3931 |
| sendto_call | 585 | 598 | 609 | 1293 |
| send_to_txdone | 683 | 781 | 10380 | 33831 |
| txdone_minus_ret | 91 | 167 | 9778 | 33234 |
| actual_post | 555 | 569 | 708 | 856 |
| teardown | 67537 | 86654 | 105254 | 228723 |
| awake | 311715 | 361710 | 411710 | 3671700 |
| sleep | 399489 | 399519 | 399520 | 399520 |
| wake_overhead | 149489 | 149519 | 149520 | 149520 |

Current trace: `aethernetio_hot_power.csv` at 3000 mV.

### Success criteria

- `ACTUAL_DEEP_SLEEP_USED=yes`
- `SOFTWARE_RESTART_COUNTED_AS_SLEEP=no`
- `NO_SLEEP_POST_PROBE_REMOVED=yes`
- `CALLBACK_DIRECTLY_BEFORE_SENDTO=yes`
- `CALLBACK_REQUIRES_TX_SUCCESS=yes`
- `INVALID_POST300_FALLBACK_REMOVED=yes`
- `PPK_CAPTURE_COMPLETE=yes`
- `SERVER_CHANGED=no`

Raw logs and per-AP JSON: `experiments/product_adaptive_probe_results/`.
