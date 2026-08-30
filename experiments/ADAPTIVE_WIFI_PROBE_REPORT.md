# Adaptive Wi-Fi Probe — campaign report

PPK source voltage for energy runs: **3000 mV** (battery converter max 3.0 V; not 3.3 V).

TCP: **disabled** in FULL quiet config (`AE_SUPPORT_TCP 0`, UDP only).

## IMPLEMENTATION

| Piece | Status |
|-------|--------|
| Server ADSL `probePing`/`deferredProbe`/`queryProbeResult` (ids 43–45) | done |
| Server `ProbeResultStore` (TTL, bounded) | done |
| Client `AuthorizedApi` 43–45 + `ProbePing` action | done |
| `WifiProbeRtcState` / CRC / select / degrade / new-network | done |
| Gateway ICMP (ESP-IDF ping; RTT discarded) | done |
| `preferred_channel` on `WiFiAp` (no BSSID assoc) | done |
| prepared_send FastPath ← probe profile + hot failure degrade | done |
| Host unit tests `test-wifi-probe` (7/7 PASS) | done |
| ADSL Java codegen regenerate + cloud deploy | **blocker** |
| Hardware chirkov / aethernetio matrix | **in progress** |

## CHIRKOV / AETHERNETIO / RECOVERY

Hardware matrix not finished in this push cycle — continuing next.

## GIT

| Repo | Branch | SHA |
|------|--------|-----|
| aether-client-cpp | `feat/adaptive-wifi-probe` | `b1092b7a` |
| aether (broker) | `feat/adaptive-wifi-probe` | `31b891f` |
| temperature-sensor | `feat/adaptive-wifi-probe` | `bff0a7f` |

Do routers select different profiles: **TBD**  
Do routers select different PRE/POST: **TBD**  
Adaptive scheme production-ready: **NO**  
remaining blockers:
1. regenerate ADSL Java stubs (`aether-protocol` gradle) and deploy cloud with methods 43–45  
2. hardware P0–P4 / PRE / POST / deferred / stale channel+IP on chirkov then aethernetio  
3. confirm UDP-only cloud path after `RebuildChannelsFromAdapters` with `AE_SUPPORT_TCP=0`
