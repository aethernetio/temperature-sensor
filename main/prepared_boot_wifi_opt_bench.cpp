/*
 * Copyright 2026 Aethernet Inc.
 *
 * Silent boot / Wi-Fi HOT-path optimization campaign (ESP32-C6).
 * Runtime variants D/G/H/E x 30 HOT; FULL between variants; 3 s deep sleep.
 * Compile-time A/B/C documented in report (one-flash campaign).
 * Metrics travel in BootWifiOptPayload 0xD8; UART is silent.
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

static constexpr std::uint8_t kVariantCount = 11;
static constexpr std::uint8_t kHotPerVariant = 30;
static constexpr std::uint8_t kMaxHotAttempts = 40;
static constexpr std::uint32_t kSleepUs = 3000000;
static constexpr std::uint32_t kRtcMagic = 0x42574F31u;  // "BWO1"
static constexpr std::uint16_t kRtcVersion = 1;

enum class Phase : std::uint16_t {
  kRegister = 0,
  kFull = 1,
  kHot = 2,
  kFinal = 3,
  kDone = 4,
};

struct VariantCfg {
  bool storage_ram;
  bool nvs_enable;
  bool force_ht20;
  std::int8_t dynamic_cs;  // -1 default, 0 off, 1 on
  std::uint8_t static_rx;
  std::uint8_t dynamic_rx;
  std::uint8_t dynamic_tx;
};

static constexpr VariantCfg kVariants[kVariantCount] = {
    {false, true, false, -1, 0, 0, 0},   // D0
    {true, true, false, -1, 0, 0, 0},    // D1
    {false, false, false, -1, 0, 0, 0},  // D2
    {true, false, false, -1, 0, 0, 0},   // D3
    {false, true, true, -1, 0, 0, 0},    // G1
    {false, true, false, 0, 0, 0, 0},    // H1
    {false, true, false, 1, 0, 0, 0},    // H2
    {false, true, false, -1, 0, 0, 16},  // E1 TX half (32->16)
    {false, true, false, -1, 0, 0, 8},   // E2 TX min-ish
    {false, true, false, -1, 5, 16, 0},  // E3 RX half
    {false, true, false, -1, 3, 8, 0},   // E4 RX min-ish
};

struct RtcState {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint16_t phase;
  std::uint8_t variant_id;
  std::uint8_t hot_index;
  std::uint8_t hot_attempt_count;
  std::uint8_t hot_send_count;
  std::uint16_t sequence_global;
  std::uint16_t next_record_id;
  std::uint32_t requested_sleep_us;
  std::uint64_t sleep_arm_rtc_us;
  std::uint8_t pending_valid;
  std::uint8_t pending_kind;
  std::uint8_t pending_variant;
  std::uint8_t pending_hot_index;
  std::uint32_t pending_user_cycle_us;
  std::uint32_t pending_wifi_cycle_us;
  std::uint32_t pending_wifi_init_us;
  std::uint32_t pending_connect_us;
  std::uint32_t pending_encode_us;
  std::uint32_t pending_txdone_us;
  std::uint32_t pending_teardown_us;
  std::uint32_t pending_heap_before;
  std::uint32_t pending_heap_after;
  std::uint8_t pending_cb_seen;
  std::uint8_t pending_cb_timeout;
  std::uint8_t pending_auth;
  std::uint8_t brownout_count;
  std::uint8_t unexpected_reset_count;
  std::uint8_t current_boot_brownout;
  std::uint8_t registered;
  std::uint8_t final_fail_count;
  std::uint8_t var_tx_success;
  std::uint8_t var_tx_fail;
  std::uint8_t var_cb_timeout;
  std::uint8_t pad0;
  std::uint32_t var_txdone_sum_us;
  std::uint8_t prev_variant_id;
  std::uint8_t prev_hot_send_count;
  std::uint8_t prev_hot_attempt_count;
  std::uint8_t prev_tx_success_count;
  std::uint8_t prev_tx_fail_count;
  std::uint8_t prev_cb_timeout_count;
  std::uint32_t prev_txdone_sum_us;
  std::uint32_t crc;
};

struct PendingDiag {
  std::uint8_t valid{0};
  std::uint8_t tx_cb_total{0};
  std::uint8_t tx_cb_success{0};
  std::uint8_t tx_cb_failed{0};
  std::uint8_t first_status{0xff};
  std::uint8_t cb_timeout{0};
  std::uint8_t disconnect_count{0};
  std::uint8_t reconnect_count{0};
  std::int8_t rssi{0};
  std::uint8_t actual_channel{0};
};

#if defined(ESP_PLATFORM)
RTC_DATA_ATTR static RtcState g_rtc{};
RTC_DATA_ATTR static prepared_send::PreparedWifiRtcCache g_rtc_wifi_cache{};
RTC_DATA_ATTR static PendingDiag g_pending_diag{};

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
  if (st.phase > static_cast<std::uint16_t>(Phase::kDone)) {
    return false;
  }
  if (st.variant_id >= kVariantCount &&
      st.phase != static_cast<std::uint16_t>(Phase::kFinal) &&
      st.phase != static_cast<std::uint16_t>(Phase::kDone)) {
    return false;
  }
  if (st.hot_index > kHotPerVariant) {
    return false;
  }
  return true;
}

static void ClearPending(RtcState& st) {
  st.pending_valid = 0;
  st.pending_kind = 0;
  st.pending_variant = 0;
  st.pending_hot_index = 0;
  st.pending_user_cycle_us = 0;
  st.pending_wifi_cycle_us = 0;
  st.pending_wifi_init_us = 0;
  st.pending_connect_us = 0;
  st.pending_encode_us = 0;
  st.pending_txdone_us = 0;
  st.pending_teardown_us = 0;
  st.pending_heap_before = 0;
  st.pending_heap_after = 0;
  st.pending_cb_seen = 0;
  st.pending_cb_timeout = 0;
  st.pending_auth = 0;
  g_pending_diag = PendingDiag{};
}

static void InitRtcFresh(Phase phase) {
  g_rtc = RtcState{};
  g_rtc.magic = kRtcMagic;
  g_rtc.version = kRtcVersion;
  g_rtc.phase = static_cast<std::uint16_t>(phase);
  g_rtc.variant_id = 0;
  g_rtc.hot_index = 1;
  g_rtc.next_record_id = 1;
  g_rtc.prev_variant_id = 0xff;
  ClearPending(g_rtc);
  SetCrc(g_rtc);
}

[[noreturn]] static void PrepareRtcStateAndDeepSleep(std::uint32_t requested_us) {
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
  (void)esp_deep_sleep_try_to_start();
  esp_deep_sleep_start();
  for (;;) {
  }
}

static void ForceFullRecovery() {
  ClearPending(g_rtc);
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kFull);
  if (g_rtc.variant_id >= kVariantCount) {
    g_rtc.variant_id = 0;
  }
  g_rtc.hot_index = 1;
  g_rtc.hot_attempt_count = 0;
  g_rtc.hot_send_count = 0;
  g_rtc.var_tx_success = 0;
  g_rtc.var_tx_fail = 0;
  g_rtc.var_cb_timeout = 0;
  g_rtc.var_txdone_sum_us = 0;
  SetCrc(g_rtc);
}

static void SnapshotPrevVariant() {
  g_rtc.prev_variant_id = g_rtc.variant_id;
  g_rtc.prev_hot_send_count = g_rtc.hot_send_count;
  g_rtc.prev_hot_attempt_count = g_rtc.hot_attempt_count;
  g_rtc.prev_tx_success_count = g_rtc.var_tx_success;
  g_rtc.prev_tx_fail_count = g_rtc.var_tx_fail;
  g_rtc.prev_cb_timeout_count = g_rtc.var_cb_timeout;
  g_rtc.prev_txdone_sum_us = g_rtc.var_txdone_sum_us;
}

static void AdvanceToNextVariantOrFinal() {
  SnapshotPrevVariant();
  if (g_rtc.variant_id + 1 < kVariantCount) {
    ++g_rtc.variant_id;
    g_rtc.phase = static_cast<std::uint16_t>(Phase::kFull);
    g_rtc.hot_index = 1;
    g_rtc.hot_attempt_count = 0;
    g_rtc.hot_send_count = 0;
    g_rtc.var_tx_success = 0;
    g_rtc.var_tx_fail = 0;
    g_rtc.var_cb_timeout = 0;
    g_rtc.var_txdone_sum_us = 0;
  } else {
    g_rtc.phase = static_cast<std::uint16_t>(Phase::kFinal);
    g_rtc.hot_index = 1;
  }
  SetCrc(g_rtc);
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

static VariantCfg const& CurrentVariant() {
  auto id = g_rtc.variant_id;
  if (id >= kVariantCount) {
    id = 0;
  }
  return kVariants[id];
}

static std::uint8_t SettingFlags(VariantCfg const& v) {
  std::uint8_t f = 0;
  if (v.storage_ram) {
    f |= 1;
  }
  if (!v.nvs_enable) {
    f |= 2;
  }
  if (v.force_ht20) {
    f |= 4;
  }
  if (v.dynamic_cs == 0) {
    f |= 8;
  }
  if (v.dynamic_cs == 1) {
    f |= 16;
  }
  if (v.dynamic_tx != 0) {
    f |= 32;
  }
  if (v.static_rx != 0 || v.dynamic_rx != 0) {
    f |= 64;
  }
  return f;
}

static ae::DataBuffer MakePayload(bench::BootWifiOptMsgType type) {
  bench::BootWifiOptPayload p{};
  p.type = static_cast<std::uint8_t>(type);
  p.variant_id = g_rtc.variant_id;
  p.hot_index = g_rtc.hot_index;
  p.sequence_global = NextSeq();
  p.record_id = g_rtc.pending_valid ? g_rtc.next_record_id : 0;
  auto const& v = CurrentVariant();
  p.reset_reason = g_early.reset_reason;
  p.wake_cause = g_early.wakeup_cause;
  p.brownout_count = g_rtc.brownout_count;
  p.sleep_elapsed_to_app_us = g_sleep_elapsed_us;
  p.sleep_to_app_overhead_us = g_sleep_overhead_us;
  p.app_entry_esp_timer_us = g_early.app_entry_esp_timer_us;
  p.setting_flags = SettingFlags(v);
  p.static_rx_buf = v.static_rx;
  p.dynamic_rx_buf = v.dynamic_rx;
  p.dynamic_tx_buf = v.dynamic_tx;
  p.dynamic_cs = v.dynamic_cs;
  std::uint8_t flags = 0;
  if (g_rtc.current_boot_brownout) {
    flags |= 1;
  }
  if (g_rtc.pending_cb_seen) {
    flags |= 2;
  }
  if (g_rtc.pending_cb_timeout) {
    flags |= 4;
  }
  p.flags = flags;
  p.prev_variant_id = g_rtc.prev_variant_id;
  p.prev_hot_send_count = g_rtc.prev_hot_send_count;
  p.prev_hot_attempt_count = g_rtc.prev_hot_attempt_count;
  p.prev_tx_success_count = g_rtc.prev_tx_success_count;
  p.prev_tx_fail_count = g_rtc.prev_tx_fail_count;
  p.prev_cb_timeout_count = g_rtc.prev_cb_timeout_count;
  p.prev_txdone_sum_us = g_rtc.prev_txdone_sum_us;
  p.prepared_message_left = static_cast<std::uint16_t>(
      prepared_send::PreparedMessageLeft() > 0xffffu
          ? 0xffffu
          : prepared_send::PreparedMessageLeft());

  if (g_rtc.pending_valid) {
    p.pending_kind = g_rtc.pending_kind;
    p.pending_variant = g_rtc.pending_variant;
    p.pending_hot_index = g_rtc.pending_hot_index;
    p.pending_user_cycle_us = g_rtc.pending_user_cycle_us;
    p.pending_wifi_cycle_us = g_rtc.pending_wifi_cycle_us;
    p.wifi_init_us = g_rtc.pending_wifi_init_us;
    p.connect_us = g_rtc.pending_connect_us;
    p.encode_send_us = g_rtc.pending_encode_us;
    p.tx_done_wait_us = g_rtc.pending_txdone_us;
    p.teardown_us = g_rtc.pending_teardown_us;
    p.heap_before_wifi = g_rtc.pending_heap_before;
    p.heap_after_wifi = g_rtc.pending_heap_after;
    p.authmode = g_rtc.pending_auth;
    if (g_pending_diag.valid) {
      p.tx_cb_total = g_pending_diag.tx_cb_total;
      p.tx_cb_success = g_pending_diag.tx_cb_success;
      p.tx_cb_failed = g_pending_diag.tx_cb_failed;
      p.first_status = g_pending_diag.first_status;
      p.cb_timeout = g_pending_diag.cb_timeout;
      p.rssi = g_pending_diag.rssi;
      p.actual_channel = g_pending_diag.actual_channel;
      p.disconnect_count = g_pending_diag.disconnect_count;
      p.reconnect_count = g_pending_diag.reconnect_count;
    }
  }
  return bench::EncodeBootWifiOpt<ae::DataBuffer>(p);
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

static void StorePendingHot(prepared_send::FastSendResult const& result,
                            std::uint32_t user_cycle_us) {
  g_rtc.pending_valid = 1;
  g_rtc.pending_kind = 2;
  g_rtc.pending_variant = g_rtc.variant_id;
  g_rtc.pending_hot_index = g_rtc.hot_index;
  g_rtc.pending_user_cycle_us = user_cycle_us;
  g_rtc.pending_wifi_cycle_us = result.cycle_us;
  g_rtc.pending_wifi_init_us = result.wifi_init_us;
  g_rtc.pending_connect_us = result.connect_us;
  g_rtc.pending_encode_us = result.encode_send_us;
  g_rtc.pending_txdone_us = result.tx_done_wait_us;
  g_rtc.pending_teardown_us = result.teardown_us;
  g_rtc.pending_heap_before = result.heap_before_wifi;
  g_rtc.pending_heap_after = result.heap_after_wifi;
  g_rtc.pending_cb_seen = result.cb_any;
  g_rtc.pending_cb_timeout = result.cb_timeout;
  g_rtc.pending_auth = result.negotiated_auth;
  g_pending_diag = PendingDiag{};
  g_pending_diag.valid = 1;
  g_pending_diag.tx_cb_total = result.tx_cb_total;
  g_pending_diag.tx_cb_success = result.tx_cb_success;
  g_pending_diag.tx_cb_failed = result.tx_cb_failed;
  g_pending_diag.first_status = result.first_status;
  g_pending_diag.cb_timeout = result.cb_timeout;
  g_pending_diag.rssi = result.rssi;
  g_pending_diag.actual_channel = result.actual_channel;
  g_pending_diag.disconnect_count = result.disconnect_count;
  g_pending_diag.reconnect_count = result.reconnect_count;
}

static void StorePendingFull(std::uint32_t user_cycle_us) {
  g_rtc.pending_valid = 1;
  g_rtc.pending_kind = 1;
  g_rtc.pending_variant = g_rtc.variant_id;
  g_rtc.pending_hot_index = 0;
  g_rtc.pending_user_cycle_us = user_cycle_us;
  g_rtc.pending_wifi_cycle_us = user_cycle_us;
  g_pending_diag = PendingDiag{};
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
      ae::AetherAppContext{}
#  if AE_DISTILLATION
          .AddAdapterFactory([&](ae::AetherAppContext const& ctx) {
            return ae::WifiAdapter::ptr::Create(
                ae::CreateWith{ctx.domain()}.with_id(
                    ae::GlobalId::kWiFiAdapter),
                ctx.aether(), ctx.poller(), ctx.dns_resolver(), kWifiInit);
          })
#  endif
  );
}

static prepared_send::FastPathConfig MakeFastConfig() {
  prepared_send::FastPathConfig c{};
  c.use_bssid = false;
  c.use_channel = true;
  c.use_fast_scan = false;
  c.use_static_ip = true;
  c.use_static_arp = true;
  c.ampdu_tx_off = false;
  c.ampdu_rx_off = false;
  c.amsdu_tx_off = false;
  c.auth = prepared_send::FastAuthMode::kWpa2;
  c.retry_max = 10;
  c.pre_delay_ms = 25;
  c.post_delay_ms = 0;
  c.post_mode = prepared_send::FastPostMode::kTxDoneCb;
  c.tx_done_wait = prepared_send::FastTxDoneWaitMode::kFirstAny;
  c.set_mac_retry_limit = false;
  auto const& v = CurrentVariant();
  c.wifi_storage_ram = v.storage_ram;
  c.wifi_nvs_enable = v.nvs_enable;
  c.force_ht20 = v.force_ht20;
  c.dynamic_cs = v.dynamic_cs;
  c.static_rx_buf_num = v.static_rx;
  c.dynamic_rx_buf_num = v.dynamic_rx;
  c.dynamic_tx_buf_num = v.dynamic_tx;
  return c;
}

static void DoFullWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto payload = MakePayload(bench::BootWifiOptMsgType::kFull);
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
  g_write_ok = false;
  g_pending_full_post_write = false;
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

static void StartFinal() {
  g_write_armed = false;
  g_write_ok = false;
  g_pending_final_exit = false;
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
                       auto client = g_client.Load();
                       g_stream = std::make_unique<ae::P2pStream>(
                           *g_app, client, kServiceUid, ae::P2pPortHandle{});
                       g_stream_sub = g_stream->stream_update_event().Subscribe(
                           []() {
                             if (!g_stream || g_write_armed) {
                               return;
                             }
                             if (!g_stream->stream_info().is_writable) {
                               return;
                             }
                             g_write_armed = true;
                             auto& wa = g_stream->Write(
                                 MakePayload(bench::BootWifiOptMsgType::kFinal));
                             g_write_sub = wa.status_event().Subscribe(
                                 [](ae::WriteAction::Status st) {
                                   g_write_ok =
                                       (st == ae::WriteAction::Status::kSuccess);
                                   g_pending_final_exit = true;
                                 });
                           });
                     });
}

static void FinishRegisterInLoop() {
  auto client = g_client.Load();
  if (!client) {
    g_app->Exit(1);
    return;
  }
  g_app->aether().Save();
  g_exit_success = true;
  g_app->Exit(0);
}

static void FinishFullPostWriteInLoop() {
  if (!g_write_ok) {
    g_app->Exit(1);
    return;
  }
  bool captured = false;
  for (int i = 0; i < 10 && !captured; ++i) {
    captured = prepared_send::CapturePreparedWifiRtcCache(&g_rtc_wifi_cache);
    if (!captured) {
      vTaskDelay(pdMS_TO_TICKS(200));
    }
  }
  bool exported = false;
  for (std::size_t n : {std::size_t{40}, std::size_t{35}, std::size_t{30}}) {
    if (prepared_send::ExportPreparedSendBlock(g_client, kServiceUid, n)) {
      exported = true;
      break;
    }
  }
  if (!exported || !captured || !prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() == 0) {
    g_app->Exit(1);
    return;
  }
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

static void AfterRegisterComplete() {
  ReleaseApp();
  g_rtc.registered = 1;
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kFull);
  g_rtc.variant_id = 0;
  g_rtc.hot_index = 1;
  ClearPending(g_rtc);
  SetCrc(g_rtc);
  PrepareRtcStateAndDeepSleep(kSleepUs);
}

static void AfterFullComplete() {
  ReleaseApp();
  prepared_send::ReleaseFullAetherWifiForHotPath();
  if (g_rtc.pending_valid) {
    AdvanceRecordIdAfterFlush();
  }
  StorePendingFull(UserCycleFromAppEntry());
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kHot);
  g_rtc.hot_index = 1;
  g_rtc.hot_attempt_count = 0;
  g_rtc.hot_send_count = 0;
  g_rtc.var_tx_success = 0;
  g_rtc.var_tx_fail = 0;
  g_rtc.var_cb_timeout = 0;
  g_rtc.var_txdone_sum_us = 0;
  SetCrc(g_rtc);
  PrepareRtcStateAndDeepSleep(kSleepUs);
}

static void AfterFinalComplete() {
  ReleaseApp();
  if (g_rtc.pending_valid) {
    AdvanceRecordIdAfterFlush();
  }
  ClearPending(g_rtc);
  g_rtc.phase = static_cast<std::uint16_t>(Phase::kDone);
  SetCrc(g_rtc);
  g_done = true;
  PrepareRtcStateAndDeepSleep(kSleepUs);
}

static void AfterFinalFailed() {
  ReleaseApp();
  if (g_rtc.final_fail_count < 255) {
    ++g_rtc.final_fail_count;
  }
  if (g_rtc.final_fail_count >= 3) {
    ClearPending(g_rtc);
    g_rtc.phase = static_cast<std::uint16_t>(Phase::kDone);
    SetCrc(g_rtc);
    g_done = true;
  }
  PrepareRtcStateAndDeepSleep(kSleepUs);
}

static void RunHotOnce() {
  if (!prepared_send::PreparedWifiRtcCacheIsValid(g_rtc_wifi_cache) ||
      !prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() == 0) {
    ForceFullRecovery();
    PrepareRtcStateAndDeepSleep(kSleepUs);
  }

  if (g_rtc.hot_attempt_count >= kMaxHotAttempts) {
    AdvanceToNextVariantOrFinal();
    PrepareRtcStateAndDeepSleep(kSleepUs);
  }

  if (g_rtc.hot_attempt_count < 255) {
    ++g_rtc.hot_attempt_count;
  }
  SetCrc(g_rtc);

  g_cfg = MakeFastConfig();
  g_wifi_snapshot =
      prepared_send::SnapshotFromPreparedWifiRtcCache(g_rtc_wifi_cache);
  auto payload = MakePayload(bench::BootWifiOptMsgType::kHot);
  auto const result =
      prepared_send::SendPreparedOnceWithFastPath(g_cfg, payload,
                                                    &g_wifi_snapshot);

  if (result.status == prepared_send::HotSendStatus::kWifiFailed) {
    SetCrc(g_rtc);
    PrepareRtcStateAndDeepSleep(kSleepUs);
  }

  if (result.status != prepared_send::HotSendStatus::kSent) {
    SetCrc(g_rtc);
    PrepareRtcStateAndDeepSleep(kSleepUs);
  }

  auto const user_cycle = UserCycleFromAppEntry();
  bool const flushed_prior = g_rtc.pending_valid != 0;
  StorePendingHot(result, user_cycle);
  if (flushed_prior) {
    AdvanceRecordIdAfterFlush();
  }

  if (g_rtc.hot_send_count < 255) {
    ++g_rtc.hot_send_count;
  }
  if (result.first_status == 1) {
    if (g_rtc.var_tx_success < 255) {
      ++g_rtc.var_tx_success;
    }
  } else if (result.first_status == 0) {
    if (g_rtc.var_tx_fail < 255) {
      ++g_rtc.var_tx_fail;
    }
  }
  if (result.cb_timeout) {
    if (g_rtc.var_cb_timeout < 255) {
      ++g_rtc.var_cb_timeout;
    }
  }
  g_rtc.var_txdone_sum_us += result.tx_done_wait_us;

  if (g_rtc.hot_index < 255) {
    ++g_rtc.hot_index;
  }

  if (g_rtc.hot_send_count >= kHotPerVariant ||
      g_rtc.hot_attempt_count >= kMaxHotAttempts) {
    AdvanceToNextVariantOrFinal();
  }
  SetCrc(g_rtc);
  PrepareRtcStateAndDeepSleep(kSleepUs);
}

static void PrepareRtcOnBoot() {
  g_early = GetExperimentEarlyEntrySnapshot();
  g_sleep_elapsed_us = 0;
  g_sleep_overhead_us = 0;
  if (g_early.valid && g_rtc.sleep_arm_rtc_us != 0 &&
      g_early.app_entry_rtc_us >= g_rtc.sleep_arm_rtc_us) {
    auto const elapsed = g_early.app_entry_rtc_us - g_rtc.sleep_arm_rtc_us;
    g_sleep_elapsed_us =
        elapsed > 0xffffffffull ? 0xffffffffu : static_cast<std::uint32_t>(elapsed);
    if (g_sleep_elapsed_us > g_rtc.requested_sleep_us) {
      g_sleep_overhead_us = g_sleep_elapsed_us - g_rtc.requested_sleep_us;
    }
  }

  auto const reset =
      static_cast<esp_reset_reason_t>(g_early.reset_reason);
  bool const valid = ValidateRtcState(g_rtc);
  g_rtc.current_boot_brownout = 0;

  if (reset == ESP_RST_BROWNOUT) {
    if (valid) {
      if (g_rtc.brownout_count < 255) {
        ++g_rtc.brownout_count;
      }
      ClearPending(g_rtc);
    } else {
      InitRtcFresh(Phase::kFull);
      g_rtc.brownout_count = 1;
    }
    g_rtc.current_boot_brownout = 1;
    ForceFullRecovery();
  } else if (!g_early.valid || reset != ESP_RST_DEEPSLEEP || !valid) {
    bool const first_poweron = (reset == ESP_RST_POWERON);
    if (first_poweron && (!valid || !g_rtc.registered)) {
      InitRtcFresh(Phase::kRegister);
    } else if (!valid) {
      InitRtcFresh(Phase::kFull);
      g_rtc.registered = 1;
      SetCrc(g_rtc);
    } else {
      if (g_rtc.unexpected_reset_count < 255) {
        ++g_rtc.unexpected_reset_count;
      }
      ForceFullRecovery();
    }
  }
  SetCrc(g_rtc);
}

#endif  // ESP_PLATFORM

}  // namespace
}  // namespace temp_sensor

#if defined(ESP_PLATFORM)

void setup() {
  using namespace temp_sensor;
  nvs_flash_init();
  g_done = false;
  g_pending_register_finish = false;
  g_pending_full_post_write = false;
  g_pending_final_exit = false;
  PrepareRtcOnBoot();
  g_cfg = MakeFastConfig();

  auto const phase = static_cast<Phase>(g_rtc.phase);
  if (phase == Phase::kDone) {
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
    } else {
      ReleaseApp();
      PrepareRtcStateAndDeepSleep(kSleepUs);
    }
    return;
  }
  if (phase == Phase::kFull) {
    if (g_exit_success) {
      AfterFullComplete();
    } else {
      ReleaseApp();
      ForceFullRecovery();
      PrepareRtcStateAndDeepSleep(kSleepUs);
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
