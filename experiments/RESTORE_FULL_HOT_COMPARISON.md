# FULL path comparison: proven E2E vs broken `esp_ordinary_full_ready_control`

| Area | `prepared_message_e2e_bench.cpp` (fb89f43) | `esp_ordinary_full_ready_control.cpp` (diag branch) |
|------|---------------------------------------------|-----------------------------------------------------|
| AetherApp | `Construct` + optional `AddAdapterFactory` (WifiAdapter, GlobalId) on distillation | `Construct` + `AdaptersFactory` lambda; may skip duplicate Wifi |
| AdapterRegistry | Factory adds one WifiAdapter; no FS_INIT channel rebuild | FS_INIT adapters + manual `channels.clear()` rebuild from live registry |
| WiFi init | Per-phase `Construct`; `PreConstructCleanup` only if `g_had_aether_app` | Similar cleanup; extra fast-path / PS overrides on diag builds |
| FS_INIT | Registration creates client via distillation (no preprovision header in E2E) | Heavy FS_INIT + `reliability_full_v1` persisted graph |
| SelectClient | Async callback; separate register vs full phases | Sync/async variants; RxTimingConf overrides on diag HOT |
| P2pStream | `P2pPortHandle{}`; no CreatePort | Mixed: ordinary uses CreatePort pattern in comments; HOT bench uses `{}` |
| Channel rebuild | **None** | **Yes** — `server->channels.clear()` + regenerate |
| FULL write | String payload `MakeBenchPayload("FULL",0)` after writable | `ReliabilityPayload` kFullReady |
| After FULL | ExportPreparedSendBlock → Save → Exit → ReleaseApp → prepared UDP loop | Export / deep sleep / HOT RTC state machine |
| Update loop | `Update` + `WaitUntil` until Exit | Blocking `PumpUntil` in setup on diag HOT (regression risk) |
| Teardown | `ReleaseApp` between phases; `ReleaseFullAetherWifiForHotPath` before prepared | Full Wi-Fi release + deep sleep between HOT |

**Do not port without proof:** `server->channels.clear()`, repeated `esp_event_loop_delete_default()`, forced RxTimingConf, stream destroy/recreate while GetCloud active, 120s pump in `setup()`.
