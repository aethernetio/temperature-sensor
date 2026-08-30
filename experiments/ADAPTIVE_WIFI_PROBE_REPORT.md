# Adaptive Wi-Fi Probe — campaign report

PPK source voltage for energy runs: **3000 mV** (battery converter max 3.0 V; not 3.3 V).

TCP: **disabled** in FULL quiet config (`AE_SUPPORT_TCP 0`, UDP only).

## IMPLEMENTATION (in progress)

| Piece | Status |
|-------|--------|
| Server ADSL `probePing`/`deferredProbe`/`queryProbeResult` (ids 43–45) | done (yaml + AuthorizedApiImpl + ProbeResultStore) |
| Client `AuthorizedApi` methods 43–45 + `ProbePing` action | done |
| `WifiProbeRtcState` / CRC / profile select / degrade / new-network | done |
| Gateway ICMP probe (ESP-IDF ping) | done (ESP only) |
| `preferred_channel` on `WiFiAp` (no BSSID assoc) | done |
| prepared_send FastPath mapping from probe profile | done |
| Hot fallback helpers | done |
| Host unit tests `test-wifi-probe` | in progress |
| ADSL Java codegen regenerate + cloud deploy | **blocker** (needs `./gradlew` protocol generate + deploy) |
| Hardware chirkov / aethernetio matrix | pending |
| Stale channel / stale IP recovery E2E | pending |

## CHIRKOV

- P0–P4 / PRE / POST / deferred / hot medians: **pending hardware**

## AETHERNETIO

- pending (password supplied in campaign script; not logged)

## RECOVERY

- pending hardware

## GIT

Branches: `feat/adaptive-wifi-probe` on:

- `aether-client-cpp-prepared-packet-v0`
- `temperature-sensor-prepared`
- `aether` (message broker)

Do routers select different profiles: **TBD**
Do routers select different PRE/POST: **TBD**
Adaptive scheme production-ready: **NO** (hardware + server codegen/deploy remaining)
remaining blockers:
- regenerate ADSL Java stubs and deploy cloud with probe methods
- UDP-only FS channel rebuild verified under `AE_SUPPORT_TCP=0`
- full chirkov + aethernetio E2E matrix
