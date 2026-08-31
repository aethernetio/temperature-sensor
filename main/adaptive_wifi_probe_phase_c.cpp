/*
 * Copyright 2026 Aethernet Inc.
 *
 * Phase C: prepared HOT + 1 s deep sleep using ICMP-winner Wi-Fi profile.
 * Existing prepared send only — no new server APIs.
 * Defaults: 5 FULL x 30 HOT, PRE=max(selected,50), POST=300, WIFI_PS_NONE.
 */

#include <cstdint>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "bench_payload.h"
#include "experiment_early_entry.h"
#include "prepared_send/prepared_send.h"

#if defined(ESP_PLATFORM)
#  include <esp_attr.h>
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_sleep.h>
#  include <esp_system.h>
#  include <esp_timer.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#  include <soc/soc_caps.h>
#endif

using namespace std::chrono_literals;

#if defined(ESP_PLATFORM)
extern "C" std::uint64_t esp_rtc_get_time_us(void);
#endif

namespace temp_sensor {
namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");

#ifndef BENCH_CLIENT_ID
#  define BENCH_CLIENT_ID "prepared_deepsleep_5x50_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("5aade50f-00d9-4624-b097-e203cdcf1e38");
#endif

#ifndef AE_PROBE_OUTER
#  define AE_PROBE_OUTER 5
#endif
#ifndef AE_PROBE_HOT_PER_OUTER
#  define AE_PROBE_HOT_PER_OUTER 30
#endif
#ifndef AE_PROBE_PROFILE
#  define AE_PROBE_PROFILE 4
#endif
#ifndef AE_PROBE_PRE_MS
#  define AE_PROBE_PRE_MS 50
#endif
#ifndef AE_PROBE_POST_MS
#  define AE_PROBE_POST_MS 300
#endif
#ifndef AE_PROBE_SLEEP_US
#  define AE_PROBE_SLEEP_US 1000000
#endif
#ifndef AE_PROBE_RUN_ID
#  define AE_PROBE_RUN_ID 1
#endif

static constexpr std::uint8_t kOuterCycles =
    static_cast<std::uint8_t>(AE_PROBE_OUTER);
static constexpr std::uint8_t kHotPerOuter =
    static_cast<std::uint8_t>(AE_PROBE_HOT_PER_OUTER);
static constexpr std::uint8_t kMaxHotAttemptsPerBlock =
    static_cast<std::uint8_t>(kHotPerOuter + 15);
static constexpr std::uint32_t kSleepUs =
    static_cast<std::uint32_t>(AE_PROBE_SLEEP_US);
static constexpr bool kNoSleep = (kSleepUs == 0);

static constexpr std::uint32_t kRtcMagic = 0x41455431u;  // "AET1"
static constexpr std::uint16_t kRtcVersion = 1;

enum class Phase : std::uint16_t {
  kRegister = 0,
  kFull = 1,
  kHot = 2,
  kFinal = 3,
  kDone = 4,
  // Cold power-on (PPK2): idle until esptool flash reset starts the campaign.
  kPowerWait = 5,
};

struct RtcState {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint16_t phase;
  std::uint8_t outer_cycle;
  std::uint8_t hot_index;
  std::uint8_t hot_attempt_count;
  std::uint8_t hot_send_count;
  std::uint16_t sequence_global;
  std::uint16_t next_record_id;
  std::uint32_t requested_sleep_us;
  std::uint64_t sleep_arm_rtc_us;
  std::uint8_t pending_valid;
  std::uint8_t pending_kind;
  std::uint8_t pending_outer;
  std::uint8_t pending_hot_index;
  std::uint32_t pending_user_cycle_us;
  std::uint32_t pending_wifi_cycle_us;
  std::uint32_t pending_connect_us;
  std::uint32_t pending_txdone_us;
  std::uint32_t pending_teardown_us;
  std::uint8_t pending_cb_seen;
  std::uint8_t pending_cb_timeout;
  std::uint8_t pending_auth;
  std::uint8_t pending_disconnect_count;
  std::uint8_t pending_last_disc_reason;
  std::uint8_t pending_reconnect_count;
  std::int8_t pending_rssi;
  std::uint8_t pending_actual_channel;
  std::uint8_t failed_assoc_wakes;
  std::uint8_t brownout_count;
  std::uint8_t unexpected_reset_count;
  std::uint8_t recovery_full_count;
  std::uint8_t current_boot_brownout;
  std::uint8_t registered;
  std::uint8_t final_fail_count;
  std::uint8_t pad0;
  std::uint16_t pad1;
  std::uint32_t crc;
};

#if defined(ESP_PLATFORM)
RTC_DATA_ATTR static RtcState g_rtc{};
RTC_DATA_ATTR static prepared_send::PreparedWifiRtcCache g_rtc_wifi_cache{};

static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

static bool g_had_aether_app = false;

static std::shared_ptr<ae::AetherApp> g_app;
static ae::Client::ptr g_client;
static std::unique_ptr<ae::P2pStream> g_stream;
static ae::Subscription g_select_sub;
static ae::Subscription g_stream_sub;
static ae::Subscription g_write_sub;

static bool g_write_armed = false;
static bool g_write_ok = false;
static bool g_exit_success = false;
static bool g_pending_register_finish = false;
static bool g_pending_full_post_write = false;
static bool g_pending_final_exit = false;
static bool g_done = false;

static ExperimentEarlyEntrySnapshot g_early{};
static std::uint32_t g_sleep_elapsed_us = 0;
static std::uint32_t g_sleep_overhead_us = 0;
static prepared_send::FastPathConfig g_cfg{};
static prepared_send::BisectWifiCacheSnapshot g_wifi_snapshot{};

static std::uint32_t Crc32Bytes(void const* data, std::size_t len) {
  auto const* p = static_cast<std::uint8_t const*>(data);
  std::uint32_t crc = 0xffffffffu;
  for (std::size_t i = 0; i < len; ++i) {
    crc ^= p[i];
    for (int b = 0; b < 8; ++b) {
      std::uint32_t const mask = -(crc & 1u);
      crc = (crc >> 1) ^ (0xedb88320u & mask);
    }
  }
  return ~crc;
}

static std::uint32_t ComputeCrc(RtcState const& st) {
  RtcState tmp = st;
  tmp.crc = 0;
  return Crc32Bytes(&tmp, sizeof(tmp));
}

static void SetCrc(RtcState& st) { st.crc = ComputeCrc(st); }

static bool ValidateRtcState(RtcState const& st) {
  if (st.magic != kRtcMagic || st.version != kRtcVersion) {
    return false;
  }
  if (ComputeCrc(st) != st.crc) {
    return false;
  }
  if (st.phase > static_cast<std::uint16_t>(Phase::kPowerWait)) {
    return false;
  }
  if (st.outer_cycle > kOuterCycles) {
    return false;
  }
  if (st.hot_index > kHotPerOuter) {
    return false;
  }
  return true;
}

static void ClearPending(RtcState& st) {
  st.pending_valid = 0;
  st.pending_kind = static_cast<std::uint8_t>(bench::DsPendingKind::kNone);
  st.pending_outer = 0;
  st.pending_hot_index = 0;
  st.pending_user_cycle_us = 0;
  st.pending_wifi_cycle_us = 0;
  st.pending_connect_us = 0;
  st.pending_txdone_us = 0;
  st.pending_teardown_us = 0;
  st.pending_cb_seen = 0;
  st.pending_cb_timeout = 0;
  st.pending_auth = 0;
  st.pending_disconnect_count = 0;
  st.pending_last_disc_reason = 0;
  st.pending_reconnect_count = 0;
  st.pending_rssi = 0;
  st.pending_actual_channel = 0;
}

static void InvalidateWifiRtcCache() {
  g_rtc_wifi_cache = prepared_send::PreparedWifiRtcCache{};
}

static void InitRtcFresh(Phase phase) {
  g_rtc = RtcState{};
  g_rtc.magic = kRtcMagic;
  g_rtc.version = kRtcVersion;
  g_rtc.phase = static_cast<std::uint16_t>(phase);
  g_rtc.outer_cycle = (phase == Phase::kFull || phase == Phase::kHot) ? 1 : 0;
  g_rtc.hot_index = 1;
  g_rtc.hot_attempt_count = 0;
  g_rtc.hot_send_count = 0;
  g_rtc.sequence_global = 0;
  g_rtc.next_record_id = 1;
  g_rtc.requested_sleep_us = 0;
  g_rtc.sleep_arm_rtc_us = 0;
  ClearPending(g_rtc);
  g_rtc.failed_assoc_wakes = 0;
  g_rtc.brownout_count = 0;
  g_rtc.unexpected_reset_count = 0;
  g_rtc.recovery_full_count = 0;
  g_rtc.current_boot_brownout = 0;
  g_rtc.registered = 0;
  g_rtc.final_fail_count = 0;
  InvalidateWifiRtcCache();
  SetCrc(g_rtc);
}

[[noreturn]] static void PrepareRtcStateAndDeepSleep(
    std::uint32_t requested_us) {
  g_rtc.requested_sleep_us = requested_us;
  esp_sleep_enable_timer_wakeup(requested_us);
  g_rtc.sleep_arm_rtc_us = esp_rtc_get_time_us();
  SetCrc(g_rtc);

#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_ON);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_ON);
#  endif

  esp_err_t const ret = esp_deep_sleep_try_to_start();
  (void)ret;
  esp_deep_sleep_start();
  for (;;) {
  }
}

static void EndCycle(std::uint32_t requested_us) {
  SetCrc(g_rtc);
  if (kNoSleep) {
    prepared_send::ReleaseFullAetherWifiForHotPath();
    vTaskDelay(pdMS_TO_TICKS(1000));
    return;
  }
  PrepareRtcStateAndDeepSleep(requested_us);
}

[[noreturn]] static void EndCycleOrDeepSleep(std::uint32_t requested_us) {
  if (kNoSleep) {
    prepared_send::ReleaseFullAetherWifiForHotPath();
    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
  }
  PrepareRtcStateAndDeepSleep(requested_us);
}

static void ForceFullRecovery() {
  ClearPending(g_rtc);
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kFull);
  if (g_rtc.outer_cycle == 0 || g_rtc.outer_cycle > kOuterCycles) {
    g_rtc.outer_cycle = 1;
  }
  g_rtc.hot_index = 1;
  g_rtc.hot_attempt_count = 0;
  g_rtc.hot_send_count = 0;
  if (g_rtc.recovery_full_count < 255) {
    ++g_rtc.recovery_full_count;
  }
  SetCrc(g_rtc);
}

static void ComputeWakeMetrics() {
  g_sleep_elapsed_us = 0;
  g_sleep_overhead_us = 0;
  auto const reset =
      static_cast<esp_reset_reason_t>(g_early.reset_reason);
  if (reset == ESP_RST_DEEPSLEEP && g_rtc.sleep_arm_rtc_us != 0 &&
      g_early.app_entry_rtc_us >= g_rtc.sleep_arm_rtc_us) {
    auto const elapsed = g_early.app_entry_rtc_us - g_rtc.sleep_arm_rtc_us;
    g_sleep_elapsed_us =
        elapsed > 0xffffffffull ? 0xffffffffu
                                : static_cast<std::uint32_t>(elapsed);
    if (g_sleep_elapsed_us > g_rtc.requested_sleep_us) {
      g_sleep_overhead_us = g_sleep_elapsed_us - g_rtc.requested_sleep_us;
    }
  }
}

static std::uint16_t NextSeq() {
  ++g_rtc.sequence_global;
  return g_rtc.sequence_global;
}

static void AdvanceRecordIdAfterFlush() {
  if (g_rtc.next_record_id < 0xffffu) {
    ++g_rtc.next_record_id;
  }
}

static void FillWakeFields(bench::DsPayload& p) {
  p.reset_reason = g_early.reset_reason;
  p.wake_cause = g_early.wakeup_cause;
  p.brownout_count = g_rtc.brownout_count;
  p.unexpected_reset_count = g_rtc.unexpected_reset_count;
  p.requested_sleep_us = g_rtc.requested_sleep_us;
  p.sleep_elapsed_to_app_us = g_sleep_elapsed_us;
  p.sleep_to_app_overhead_us = g_sleep_overhead_us;
  p.app_entry_esp_timer_us =
      g_early.app_entry_esp_timer_us < 0
          ? 0
          : static_cast<std::uint32_t>(g_early.app_entry_esp_timer_us);

  std::uint8_t flags = 0;
  if (g_rtc.current_boot_brownout) {
    flags |= static_cast<std::uint8_t>(bench::DsFlags::kBrownout);
  }
  if (prepared_send::PreparedWifiRtcCacheIsValid(g_rtc_wifi_cache)) {
    flags |= static_cast<std::uint8_t>(bench::DsFlags::kCacheValid);
  }
  if (ValidateRtcState(g_rtc)) {
    flags |= static_cast<std::uint8_t>(bench::DsFlags::kStateValid);
  }
  p.flags = flags;
  p.failed_assoc_wakes = g_rtc.failed_assoc_wakes;
}

static void FillPendingFields(bench::DsPayload& p) {
  if (!g_rtc.pending_valid) {
    p.pending_kind = static_cast<std::uint8_t>(bench::DsPendingKind::kNone);
    p.pending_outer = 0;
    p.pending_hot_index = 0;
    p.pending_user_cycle_us = 0;
    p.pending_wifi_cycle_us = 0;
    p.connect_us = 0;
    p.tx_done_wait_us = 0;
    p.teardown_us = 0;
    p.negotiated_auth = 0;
    p.disconnect_count = 0;
    p.last_disconnect_reason = 0;
    p.reconnect_count = 0;
    p.rssi = 0;
    p.actual_channel = 0;
    return;
  }
  p.pending_kind = g_rtc.pending_kind;
  p.pending_outer = g_rtc.pending_outer;
  p.pending_hot_index = g_rtc.pending_hot_index;
  p.pending_user_cycle_us = g_rtc.pending_user_cycle_us;
  p.pending_wifi_cycle_us = g_rtc.pending_wifi_cycle_us;
  p.connect_us = g_rtc.pending_connect_us;
  p.tx_done_wait_us = g_rtc.pending_txdone_us;
  p.teardown_us = g_rtc.pending_teardown_us;
  p.negotiated_auth = g_rtc.pending_auth;
  p.disconnect_count = g_rtc.pending_disconnect_count;
  p.last_disconnect_reason = g_rtc.pending_last_disc_reason;
  p.reconnect_count = g_rtc.pending_reconnect_count;
  p.rssi = g_rtc.pending_rssi;
  p.actual_channel = g_rtc.pending_actual_channel;
  if (g_rtc.pending_cb_seen) {
    p.flags |= static_cast<std::uint8_t>(bench::DsFlags::kCallbackSeen);
  }
  if (g_rtc.pending_cb_timeout) {
    p.flags |= static_cast<std::uint8_t>(bench::DsFlags::kCallbackTimeout);
  }
}

static ae::DataBuffer MakeDsPayload(bench::DsMsgType type) {
  bench::DsPayload p{};
  p.type = static_cast<std::uint8_t>(type);
  p.outer_cycle = g_rtc.outer_cycle;
  p.hot_index = g_rtc.hot_index;
  p.sequence_global = NextSeq();
  // Assign id without advancing until the send that flushes pending succeeds
  // (HOT Wi-Fi retries must reuse the same record_id).
  p.record_id = g_rtc.pending_valid ? g_rtc.next_record_id : 0;
  FillWakeFields(p);
  FillPendingFields(p);
  p.prepared_message_left =
      static_cast<std::uint16_t>(prepared_send::PreparedMessageLeft() > 0xffffu
                                     ? 0xffffu
                                     : prepared_send::PreparedMessageLeft());
  return bench::EncodeDs<ae::DataBuffer>(p);
}

static void StorePendingFull(std::uint32_t user_cycle_us) {
  g_rtc.pending_valid = 1;
  g_rtc.pending_kind = static_cast<std::uint8_t>(bench::DsPendingKind::kFull);
  g_rtc.pending_outer = g_rtc.outer_cycle;
  g_rtc.pending_hot_index = 0;
  g_rtc.pending_user_cycle_us = user_cycle_us;
  g_rtc.pending_wifi_cycle_us = user_cycle_us;
  g_rtc.pending_connect_us = 0;
  g_rtc.pending_txdone_us = 0;
  g_rtc.pending_teardown_us = 0;
  g_rtc.pending_cb_seen = 0;
  g_rtc.pending_cb_timeout = 0;
  g_rtc.pending_auth = 0;
  g_rtc.pending_disconnect_count = 0;
  g_rtc.pending_last_disc_reason = 0;
  g_rtc.pending_reconnect_count = 0;
  g_rtc.pending_rssi = 0;
  g_rtc.pending_actual_channel = 0;
}

static void StorePendingHot(prepared_send::FastSendResult const& result,
                            std::uint32_t user_cycle_us) {
  g_rtc.pending_valid = 1;
  g_rtc.pending_kind = static_cast<std::uint8_t>(bench::DsPendingKind::kHot);
  g_rtc.pending_outer = g_rtc.outer_cycle;
  g_rtc.pending_hot_index = g_rtc.hot_index;
  g_rtc.pending_user_cycle_us = user_cycle_us;
  g_rtc.pending_wifi_cycle_us = result.cycle_us;
  g_rtc.pending_connect_us = result.connect_us;
  g_rtc.pending_txdone_us = result.tx_done_wait_us;
  g_rtc.pending_teardown_us = result.teardown_us;
  g_rtc.pending_cb_seen = result.cb_any;
  g_rtc.pending_cb_timeout = result.cb_timeout;
  g_rtc.pending_auth = result.negotiated_auth;
  g_rtc.pending_disconnect_count = result.disconnect_count;
  g_rtc.pending_last_disc_reason = result.last_disconnect_reason;
  g_rtc.pending_reconnect_count = result.reconnect_count;
  g_rtc.pending_rssi = result.rssi;
  g_rtc.pending_actual_channel = result.actual_channel;
}

static void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

static void PreConstructCleanup() {
  if (!g_had_aether_app) {
    return;
  }
#  if !AE_WIFI_USE_FULL_DEINIT
  esp_netif_deinit();
  esp_event_loop_delete_default();
#  endif
}

static void ConstructAether() {
  PreConstructCleanup();
  g_had_aether_app = true;
  g_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}.AdaptersFactory(
          [&](ae::AetherAppContext const& ctx) {
            auto ap = ae::AdapterRegistry::ptr{ctx.aether()->adapter_registry};
            if (!ap.is_valid()) {
              ap = ae::AdapterRegistry::ptr::Create(
                  ae::CreateWith{ctx.domain()}
                      .with_id(ae::GlobalId::kAdapterRegistry)
                      .with_flags(ae::ObjFlags::kUnloadedByDefault));
            }
            auto loaded_reg = ap.Load();
            assert(loaded_reg && "AdapterRegistry load failed");
            loaded_reg->Clear();
            loaded_reg->Add(ae::WifiAdapter::ptr::Create(
                ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
                ctx.dns_resolver(), kWifiInit));
            return ap;
          }));
}

static prepared_send::FastPathConfig MakeFastConfig() {
  auto const profile =
      static_cast<ae::WifiProbeProfile>(AE_PROBE_PROFILE);
  std::uint16_t pre = static_cast<std::uint16_t>(AE_PROBE_PRE_MS);
  // First reliability sleep campaign: safety margin max(selected, 50).
  if (pre < 50) {
    pre = 50;
  }
  auto c = prepared_send::FastPathConfigForProbeProfile(
      profile, pre, static_cast<std::uint16_t>(AE_PROBE_POST_MS));
  c.use_bssid = false;
  c.wifi_storage_ram = true;
  c.wifi_nvs_enable = true;
  c.auth = prepared_send::FastAuthMode::kWpa2;
  c.retry_max = 10;
  // TX-done callback + POST delay (default 300 ms).
  c.post_mode = prepared_send::FastPostMode::kTxDoneCb;
  c.post_delay_ms = static_cast<std::uint16_t>(AE_PROBE_POST_MS);
  return c;
}

static void DoFullWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto payload = MakeDsPayload(bench::DsMsgType::kFull);
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    g_write_ok = (st == ae::WriteAction::Status::kSuccess);
    g_pending_full_post_write = true;
  });
}

static void MaybeFullWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  DoFullWrite();
}

static void OnFullClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  auto client = g_client.Load();
  auto cloud = client->cloud().Load();
  if (cloud) {
    for (auto& [sid, cs] : cloud->servers()) {
      auto server = cs.server.Load();
      if (server) {
        server->RebuildChannelsFromAdapters();
      }
    }
  }
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             ae::P2pPortHandle{});
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeFullWrite(); });
  MaybeFullWrite();
}

static void StartRegister() {
  g_write_armed = false;
  g_pending_register_finish = false;
  g_exit_success = false;
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       g_client = std::move(res).value();
                       g_pending_register_finish = true;
                     });
}

static void StartFull() {
  g_write_armed = false;
  g_pending_full_post_write = false;
  g_write_ok = false;
  g_exit_success = false;
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       OnFullClientReady(std::move(res).value());
                     });
}

static void DoFinalWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto& wa = g_stream->Write(MakeDsPayload(bench::DsMsgType::kFinal));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    g_write_ok = (st == ae::WriteAction::Status::kSuccess);
    g_pending_final_exit = true;
  });
}

static void MaybeFinalWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  DoFinalWrite();
}

static void OnFinalClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  auto client = g_client.Load();
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             ae::P2pPortHandle{});
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeFinalWrite(); });
  MaybeFinalWrite();
}

static void StartFinal() {
  g_write_armed = false;
  g_pending_final_exit = false;
  g_write_ok = false;
  g_exit_success = false;
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       OnFinalClientReady(std::move(res).value());
                     });
}

static void FinishRegisterInLoop() {
  g_app->aether().Save();
  g_exit_success = true;
  g_app->Exit(0);
}

static void FinishFullPostWriteInLoop() {
  if (!g_write_ok) {
    g_app->Exit(1);
    return;
  }
  if (!prepared_send::CapturePreparedWifiRtcCache(&g_rtc_wifi_cache)) {
    g_app->Exit(1);
    return;
  }
  if (!prepared_send::ExportPreparedSendBlock(g_client, kServiceUid,
                                              kHotPerOuter)) {
    g_app->Exit(1);
    return;
  }
  if (!prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() !=
          static_cast<std::uint32_t>(kHotPerOuter)) {
    g_app->Exit(1);
    return;
  }
  g_app->aether().Save();
  g_exit_success = true;
  g_app->Exit(0);
}

static void FinishFinalInLoop() {
  if (!g_write_ok) {
    g_app->Exit(1);
    return;
  }
  g_exit_success = true;
  g_app->Exit(0);
}

static std::uint32_t UserCycleFromAppEntry() {
  auto const now = esp_timer_get_time();
  auto const entry = g_early.app_entry_esp_timer_us;
  if (now < entry) {
    return 0;
  }
  auto const delta = now - entry;
  return delta > 0xffffffffll ? 0xffffffffu
                              : static_cast<std::uint32_t>(delta);
}

static void AfterRegisterComplete() {
  ReleaseApp();
  g_rtc.registered = 1;
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kFull);
  g_rtc.outer_cycle = 1;
  g_rtc.hot_index = 1;
  g_rtc.hot_attempt_count = 0;
  g_rtc.hot_send_count = 0;
  ClearPending(g_rtc);
  SetCrc(g_rtc);
  if (kNoSleep) {
    return;
  }
  EndCycleOrDeepSleep(kSleepUs);
}

static void AfterFullComplete() {
  ReleaseApp();
  prepared_send::ReleaseFullAetherWifiForHotPath();
  if (g_rtc.pending_valid) {
    AdvanceRecordIdAfterFlush();
  }
  auto const user_cycle = UserCycleFromAppEntry();
  StorePendingFull(user_cycle);
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kHot);
  g_rtc.hot_index = 1;
  g_rtc.hot_attempt_count = 0;
  g_rtc.hot_send_count = 0;
  SetCrc(g_rtc);
  if (kNoSleep) {
    return;
  }
  EndCycleOrDeepSleep(kSleepUs);
}

static void AfterFinalComplete() {
  ReleaseApp();
  if (g_rtc.pending_valid) {
    AdvanceRecordIdAfterFlush();
  }
  ClearPending(g_rtc);
  g_rtc.final_fail_count = 0;
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kDone);
  SetCrc(g_rtc);
  g_done = true;
  EndCycleOrDeepSleep(kSleepUs);
}

static void AfterFinalFailed() {
  ReleaseApp();
  if (g_rtc.final_fail_count < 255) {
    ++g_rtc.final_fail_count;
  }
  // After several Aether FINAL failures, stop the campaign so metrics already
  // delivered (via pending on HOT/FULL) are not blocked forever.
  if (g_rtc.final_fail_count >= 5) {
    ClearPending(g_rtc);
    g_rtc.phase = static_cast<std::uint16_t>(Phase::kDone);
    SetCrc(g_rtc);
    g_done = true;
    EndCycleOrDeepSleep(kSleepUs);
  }
  SetCrc(g_rtc);
  EndCycleOrDeepSleep(kSleepUs);
}

static bool WifiFailedBeforeEncode(prepared_send::FastSendResult const& r) {
  if (r.status == prepared_send::HotSendStatus::kWifiFailed) {
    return true;
  }
  bool const encode_ok =
      (r.status_flags &
       static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk)) != 0;
  return !encode_ok && r.status != prepared_send::HotSendStatus::kSent;
}

static void RunHotOnce() {
  if (!prepared_send::PreparedWifiRtcCacheIsValid(g_rtc_wifi_cache)) {
    ForceFullRecovery();
    EndCycleOrDeepSleep(kSleepUs);
  }
  g_wifi_snapshot =
      prepared_send::SnapshotFromPreparedWifiRtcCache(g_rtc_wifi_cache);
  if (!g_wifi_snapshot.valid_ip || g_wifi_snapshot.channel == 0) {
    ForceFullRecovery();
    EndCycleOrDeepSleep(kSleepUs);
  }
  if (!prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() == 0) {
    ForceFullRecovery();
    EndCycleOrDeepSleep(kSleepUs);
  }
  if (g_rtc.hot_attempt_count >= kMaxHotAttemptsPerBlock) {
    ForceFullRecovery();
    EndCycleOrDeepSleep(kSleepUs);
  }

  if (g_rtc.hot_attempt_count < 255) {
    ++g_rtc.hot_attempt_count;
  }
  SetCrc(g_rtc);

  auto payload = MakeDsPayload(bench::DsMsgType::kHot);
  auto const result =
      prepared_send::SendPreparedOnceWithFastPath(g_cfg, payload,
                                                    &g_wifi_snapshot);

  if (result.status == prepared_send::HotSendStatus::kSent) {
    auto const user_cycle = UserCycleFromAppEntry();
    if (g_rtc.hot_send_count < 255) {
      ++g_rtc.hot_send_count;
    }
    bool const flushed_prior = g_rtc.pending_valid != 0;
    StorePendingHot(result, user_cycle);
    if (flushed_prior) {
      AdvanceRecordIdAfterFlush();
    }

    if (g_rtc.hot_index < 255) {
      ++g_rtc.hot_index;
    }
    if (g_rtc.hot_index > kHotPerOuter) {
      if (g_rtc.outer_cycle < kOuterCycles) {
        ++g_rtc.outer_cycle;
        g_rtc.phase = static_cast<std::uint16_t>(Phase::kFull);
        g_rtc.hot_index = 1;
        g_rtc.hot_attempt_count = 0;
        g_rtc.hot_send_count = 0;
      } else {
        g_rtc.phase = static_cast<std::uint16_t>(Phase::kFinal);
      }
    }
    SetCrc(g_rtc);
    EndCycle(kSleepUs);
  }

  if (WifiFailedBeforeEncode(result)) {
    if (g_rtc.failed_assoc_wakes < 255) {
      ++g_rtc.failed_assoc_wakes;
    }
    SetCrc(g_rtc);
    EndCycle(kSleepUs);
    return;
  }

  // Encode/send failure after Wi-Fi: do not advance; retry same index.
  SetCrc(g_rtc);
  EndCycle(kSleepUs);
}

static bool IsColdPowerOn(esp_reset_reason_t reset) {
  return reset == ESP_RST_POWERON;
}

static bool IsProvisioningReset(esp_reset_reason_t reset) {
  switch (reset) {
    case ESP_RST_SW:
    case ESP_RST_USB:
    case ESP_RST_EXT:
      return true;
    default:
      return false;
  }
}

static void PrepareRtcOnBoot() {
  g_early = GetExperimentEarlyEntrySnapshot();
  auto const reset =
      static_cast<esp_reset_reason_t>(g_early.reset_reason);
  bool const valid = ValidateRtcState(g_rtc);

  g_rtc.current_boot_brownout = 0;

  if (IsColdPowerOn(reset)) {
    InvalidateWifiRtcCache();
    InitRtcFresh(Phase::kPowerWait);
    ComputeWakeMetrics();
    SetCrc(g_rtc);
    return;
  }

  if (valid && static_cast<Phase>(g_rtc.phase) == Phase::kPowerWait) {
    if (IsProvisioningReset(reset)) {
      InvalidateWifiRtcCache();
      InitRtcFresh(Phase::kRegister);
    } else {
      ComputeWakeMetrics();
      SetCrc(g_rtc);
      return;
    }
  }

  if (reset == ESP_RST_BROWNOUT) {
    if (valid) {
      if (g_rtc.brownout_count < 255) {
        ++g_rtc.brownout_count;
      }
      ClearPending(g_rtc);
    } else {
      InitRtcFresh(Phase::kFull);
      g_rtc.brownout_count = 1;
      g_rtc.outer_cycle = 1;
    }
    g_rtc.current_boot_brownout = 1;
    ForceFullRecovery();
  } else if (reset != ESP_RST_DEEPSLEEP || !valid) {
    if (g_rtc.magic != kRtcMagic) {
      InvalidateWifiRtcCache();
    }
    if (!valid || !g_rtc.registered) {
      InitRtcFresh(Phase::kRegister);
    } else {
      if (g_rtc.unexpected_reset_count < 255) {
        ++g_rtc.unexpected_reset_count;
      }
      ForceFullRecovery();
    }
  }
  // else: DEEPSLEEP + valid — continue phase as stored

  ComputeWakeMetrics();
  SetCrc(g_rtc);
}

#endif  // ESP_PLATFORM

}  // namespace
}  // namespace temp_sensor

#if defined(ESP_PLATFORM)

void setup() {
  using namespace temp_sensor;
  nvs_flash_init();
  g_cfg = MakeFastConfig();
  g_done = false;
  g_pending_register_finish = false;
  g_pending_full_post_write = false;
  g_pending_final_exit = false;
  PrepareRtcOnBoot();

  auto const phase = static_cast<Phase>(g_rtc.phase);
  if (phase == Phase::kDone || phase == Phase::kPowerWait) {
    g_done = true;
    return;
  }
  if (phase == Phase::kRegister) {
    StartRegister();
    return;
  }
  if (phase == Phase::kFull) {
    StartFull();
    return;
  }
  if (phase == Phase::kFinal) {
    StartFinal();
    return;
  }
  // HOT is handled synchronously in loop().
}

void loop() {
  using namespace temp_sensor;
  if (g_done) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    return;
  }

  auto const phase = static_cast<Phase>(g_rtc.phase);
  if (phase == Phase::kHot) {
    RunHotOnce();
    return;
  }

  auto process_deferred = []() {
    if (g_app && g_pending_register_finish) {
      g_pending_register_finish = false;
      FinishRegisterInLoop();
      return true;
    }
    if (g_app && g_pending_full_post_write) {
      g_pending_full_post_write = false;
      FinishFullPostWriteInLoop();
      return true;
    }
    if (g_app && g_pending_final_exit) {
      g_pending_final_exit = false;
      FinishFinalInLoop();
      return true;
    }
    return false;
  };

  if (process_deferred()) {
    return;
  }

  if (!g_app) {
    return;
  }

  if (!g_app->IsExited()) {
    auto t = g_app->Update(ae::Now());
    if (process_deferred()) {
      return;
    }
    if (!g_app->IsExited()) {
      g_app->WaitUntil(t);
    }
    return;
  }

  if (phase == Phase::kRegister) {
    if (g_exit_success) {
      AfterRegisterComplete();
      if (kNoSleep) {
        StartFull();
      }
    } else {
      ReleaseApp();
      EndCycleOrDeepSleep(kSleepUs);
    }
    return;
  }
  if (phase == Phase::kFull) {
    if (g_exit_success) {
      AfterFullComplete();
    } else {
      ReleaseApp();
      ForceFullRecovery();
      EndCycleOrDeepSleep(kSleepUs);
    }
    return;
  }
  if (phase == Phase::kFinal) {
    if (g_exit_success) {
      AfterFinalComplete();
    } else {
      AfterFinalFailed();
    }
    return;
  }
}

#else

void setup() {}
void loop() {}

#endif
