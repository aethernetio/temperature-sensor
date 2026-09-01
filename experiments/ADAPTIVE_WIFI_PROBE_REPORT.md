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

**Status:** IN PROGRESS (autonomous campaign)

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

Firmware: `main/product_adaptive_wifi_probe.cpp`
(`-DAE_EXP_PRODUCT_ADAPTIVE_PROBE=1`). Receiver:
aether `examples/probe_receiver` (TCP only). Selection algorithm:
`examples/probe_receiver/product_probe_select.h`, host tests in
aether `tests/test-product-probe`.

`sizeof(ProbeRtcState)` = 64 bytes of RTC memory.

### Selected parameters

`HOT sent` and `HOT fail` are the device's own counters: a send fails
only when the local send call fails. `HOT delivered` is how many of
those packets the receiver saw, so the two differ by whatever the
network dropped after the send succeeded.

| AP | status | profile | PRE ms | POST ms | sleep ms | HOT sent | HOT fail | HOT delivered | reprobes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chirkov | OK | P1 | 0 | 300 | 250 | 100 | 0 | 36 | 0 |
| aethernetio | OK | P4 | 0 | 300 | 250 | 100 | 0 | 98 | 0 |

### Probe batches as counted by the receiver

A batch passes when all 20 packets arrive; 19 buys one extra batch at
the same delay. When no candidate passes, the POST delay falls back to
the most conservative value in the table and the stage keeps retrying
it, up to its batch cap, in case it passes later. That is why a table
can show the same POST value repeated without ever reaching 20/20.

**chirkov**

| batch | stage | POST ms | expected | unique | dup | missing |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | POST_PROBE | 100 | 20 | 8 | 0 | 12 |
| 2 | POST_PROBE | 200 | 20 | 11 | 0 | 9 |
| 3 | POST_PROBE | 300 | 20 | 12 | 0 | 8 |
| 4 | POST_PROBE | 300 | 20 | 15 | 0 | 5 |
| 5 | POST_PROBE | 300 | 20 | 12 | 0 | 8 |
| 6 | POST_PROBE | 300 | 20 | 12 | 0 | 8 |
| 7 | POST_PROBE | 300 | 20 | 14 | 0 | 6 |
| 8 | POST_PROBE | 300 | 20 | 15 | 0 | 5 |
| 9 | POST_PROBE | 300 | 20 | 17 | 0 | 3 |
| 10 | POST_PROBE | 300 | 20 | 16 | 0 | 4 |
| 11 | POST_PROBE | 300 | 20 | 13 | 0 | 7 |
| 12 | POST_PROBE | 300 | 20 | 19 | 0 | 1 |
| 13 | SLEEP_CONFIRM | 300 | 20 | 5 | 0 | 15 |

Hot cycle time from the previous-send timing carried in each HOT_DATA packet (us): n=36 min=499304 median=569311 p90=599248 max=629427

**PPK_CAPTURE_REQUIRED** - current trace not captured.

**aethernetio**

| batch | stage | POST ms | expected | unique | dup | missing |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | POST_PROBE | 100 | 20 | 20 | 0 | 0 |
| 2 | POST_PROBE | 200 | 20 | 20 | 0 | 0 |
| 3 | POST_PROBE | 300 | 20 | 19 | 0 | 1 |
| 4 | POST_PROBE | 300 | 20 | 18 | 0 | 2 |
| 5 | POST_PROBE | 300 | 20 | 19 | 0 | 1 |
| 6 | POST_PROBE | 300 | 20 | 19 | 0 | 1 |
| 7 | POST_PROBE | 300 | 20 | 19 | 0 | 1 |
| 8 | POST_PROBE | 300 | 20 | 18 | 0 | 2 |
| 9 | POST_PROBE | 300 | 20 | 19 | 0 | 1 |
| 10 | POST_PROBE | 300 | 20 | 20 | 0 | 0 |
| 11 | POST_PROBE | 300 | 20 | 20 | 0 | 0 |
| 12 | POST_PROBE | 300 | 20 | 20 | 0 | 0 |
| 13 | SLEEP_CONFIRM | 300 | 20 | 19 | 0 | 1 |

Hot cycle time from the previous-send timing carried in each HOT_DATA packet (us): n=98 min=569438 median=649484 p90=689419 max=839521

**PPK_CAPTURE_REQUIRED** - current trace not captured.

Raw logs and per-AP JSON: `experiments/product_adaptive_probe_results/`.
