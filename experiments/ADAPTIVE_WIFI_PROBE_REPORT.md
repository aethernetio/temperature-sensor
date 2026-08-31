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
| git status clean | yes |
| methods 43–45 / ProbeResultStore | absent |

## CHIRKOV — Phase A (ICMP, complete)

| Profile | connect median | ICMP loss | min stable PRE |
|---------|----------------|-----------|----------------|
| P0 DEFAULT | 1709 ms | 0.00% | 0 ms |
| P1 CACHED_IP | 115 ms | 0.00% | 0 ms (1.67% at PRE=0 → keep higher if needed) |
| P2 CHANNEL | 1638 ms | 4.44% | FAIL baseline |
| P3 CHANNEL_IP | **96 ms** | **0.00%** | **0 ms** |
| P4 CHANNEL_IP_ARP | 98 ms | 1.11% | 300 ms (PRE=200 failed 3.33%) |

**Winner (reliability-first): P3 CHANNEL_IP, PRE=0 ms**

Fingerprint (diag): SSID=chirkov, channel=9, BSSID=30:68:93:39:02:74, gw=192.168.68.1

Artifacts: `experiments/adaptive_probe_results/chirkov_phase_a.json`

## CHIRKOV — Phase B (existing Ping)

In progress — 50 cold `AuthorizedApi::ping` cycles, client `reliability_full_v1`, FS_INIT preprovision.

Profile-fast FULL path with cached IP/ARP: **NOT_TESTED** (requires invasive Wi-Fi lifecycle bypass beyond `preferred_channel`).

## CHIRKOV — Phase C (prepared HOT + 1 s sleep)

Pending after Phase B. Config: profile=P3, PRE=max(0,50)=50 ms, POST=300 ms baseline, 5×30 HOT + POST search + long run.

## AETHERNETIO

Pending after chirkov A→B→C (AP cache invalidate on switch).

## GIT (interim)

| Repo | Branch | SHA |
|------|--------|-----|
| aether (broker) | `feat/adaptive-wifi-probe` | `692f0f1` |
| aether-client-cpp | `feat/adaptive-wifi-probe` | `586ba2ac` |
| temperature-sensor | `feat/adaptive-wifi-probe` | local WIP |
