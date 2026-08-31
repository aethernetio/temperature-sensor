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
