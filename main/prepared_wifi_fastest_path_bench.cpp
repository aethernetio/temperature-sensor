/*
 * Copyright 2026 Aethernet Inc.
 *
 * Silent fastest-path prepared reconnect bench (ESP32-C6).
 * One compile-time variant per flash. Results travel in FastPayload.
 *
 * BASE: cached channel + static IPv4 + static ARP, no BSSID.
 * Wi-Fi 4, WIFI_PS_NONE, auto PHY, max TX power. 1 s gap outside timer.
 */

#include <cstdint>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "bench_payload.h"
#include "prepared_send/prepared_send.h"

#if defined(ESP_PLATFORM)
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_timer.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#endif

using namespace std::chrono_literals;

namespace temp_sensor {
namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");

#ifndef BENCH_CLIENT_ID
#  define BENCH_CLIENT_ID "prepared_wifi_bisect_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("3d284a4f-ebb4-451e-a2c5-aecb0d647a45");
#endif

#ifndef AE_EXP_FAST_N
#  define AE_EXP_FAST_N 20
#endif
#ifndef AE_EXP_FAST_TEST_ID
#  define AE_EXP_FAST_TEST_ID 0
#endif
#ifndef AE_EXP_FAST_PRE_MS
#  define AE_EXP_FAST_PRE_MS 200
#endif
#ifndef AE_EXP_FAST_POST_MS
#  define AE_EXP_FAST_POST_MS 300
#endif
#ifndef AE_EXP_FAST_USE_BSSID
#  define AE_EXP_FAST_USE_BSSID 0
#endif
#ifndef AE_EXP_FAST_FAST_SCAN
#  define AE_EXP_FAST_FAST_SCAN 0
#endif
#ifndef AE_EXP_FAST_AUTH
#  define AE_EXP_FAST_AUTH 0
#endif
#ifndef AE_EXP_FAST_RETRY
#  define AE_EXP_FAST_RETRY 10
#endif
#ifndef AE_EXP_FAST_POST_MODE
#  define AE_EXP_FAST_POST_MODE 0
#endif
#ifndef AE_EXP_FAST_AMPDU_TX_OFF
#  define AE_EXP_FAST_AMPDU_TX_OFF 0
#endif
#ifndef AE_EXP_FAST_STORAGE_RAM
#  define AE_EXP_FAST_STORAGE_RAM 0
#endif

static constexpr int kPreparedPerVariant = AE_EXP_FAST_N;
static constexpr int kPreparedGapMs = 1000;
static constexpr std::uint8_t kTestId =
    static_cast<std::uint8_t>(AE_EXP_FAST_TEST_ID);

#if defined(ESP_PLATFORM)
static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

static bool g_had_aether_app = false;

static void PreConstructCleanup() {
  if (!g_had_aether_app) {
    return;
  }
#  if !AE_WIFI_USE_FULL_DEINIT
  esp_netif_deinit();
  esp_event_loop_delete_default();
#  endif
}

static std::int64_t NowUs() { return esp_timer_get_time(); }
#else
static std::int64_t NowUs() { return 0; }
#endif

enum class Phase : std::uint8_t {
  kRegister,
  kFullCycle,
  kPrepared,
  kFinal,
  kDone,
};

static std::shared_ptr<ae::AetherApp> g_app;
static ae::Client::ptr g_client;
static std::unique_ptr<ae::P2pStream> g_stream;
static ae::Subscription g_select_sub;
static ae::Subscription g_stream_sub;
static ae::Subscription g_write_sub;

static Phase g_phase = Phase::kRegister;
static bool g_registration_pending = false;
static bool g_write_armed = false;
static bool g_done = false;

static bool g_pending_register_finish = false;
static bool g_pending_full_post_write = false;
static bool g_full_write_ok = false;
static bool g_pending_final_exit = false;

static int g_prepared_index = 0;
static bool g_prepared_waiting_gap = false;
#if defined(ESP_PLATFORM)
static TickType_t g_prepared_gap_until = 0;
#endif

static std::uint16_t g_seq = 0;
static std::uint32_t g_registration_us = 0;
static std::uint32_t g_pending_full_us = 0;
static bool g_have_pending_full = false;

static std::uint16_t g_wifi_ready_count = 0;
static std::uint16_t g_encode_count = 0;
static std::uint16_t g_sendto_count = 0;
static std::uint16_t g_nonce_start = 0;

static prepared_send::FastSendResult g_last_result{};
static prepared_send::BisectWifiCacheSnapshot g_cache{};
static prepared_send::FastPathConfig g_cfg{};
static std::uint8_t g_assoc_bits = 0;

static std::int64_t g_t0 = 0;

static void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

static std::uint16_t NextSeq() { return ++g_seq; }

static prepared_send::FastPathConfig MakeFastConfig() {
  prepared_send::FastPathConfig c{};
  c.use_bssid = AE_EXP_FAST_USE_BSSID != 0;
  c.use_channel = true;
  c.use_fast_scan = AE_EXP_FAST_FAST_SCAN != 0;
  c.use_static_ip = true;
  c.use_static_arp = true;
  c.ampdu_tx_off = AE_EXP_FAST_AMPDU_TX_OFF != 0;
  c.wifi_storage_ram = AE_EXP_FAST_STORAGE_RAM != 0;
  c.auth = static_cast<prepared_send::FastAuthMode>(AE_EXP_FAST_AUTH);
  c.retry_max = static_cast<std::uint8_t>(AE_EXP_FAST_RETRY);
  c.pre_delay_ms = static_cast<std::uint16_t>(AE_EXP_FAST_PRE_MS);
  c.post_delay_ms = static_cast<std::uint16_t>(AE_EXP_FAST_POST_MS);
  c.post_mode = static_cast<prepared_send::FastPostMode>(AE_EXP_FAST_POST_MODE);
  return c;
}

static std::uint8_t AssocBitsOf(prepared_send::FastPathConfig const& c) {
  using F = bench::FastAssocBits;
  std::uint8_t bits = 0;
  if (c.use_bssid) {
    bits |= static_cast<std::uint8_t>(F::kBssid);
  }
  if (c.use_channel) {
    bits |= static_cast<std::uint8_t>(F::kChannel);
  }
  if (c.use_fast_scan) {
    bits |= static_cast<std::uint8_t>(F::kFastScan);
  }
  if (c.use_static_ip) {
    bits |= static_cast<std::uint8_t>(F::kStaticIp);
  }
  if (c.use_static_arp) {
    bits |= static_cast<std::uint8_t>(F::kStaticArp);
  }
  if (c.ampdu_tx_off) {
    bits |= static_cast<std::uint8_t>(F::kAmpduTxOff);
  }
  if (c.wifi_storage_ram) {
    bits |= static_cast<std::uint8_t>(F::kStorageRam);
  }
  if (c.post_mode != prepared_send::FastPostMode::kFixedDelay) {
    bits |= static_cast<std::uint8_t>(F::kCallback);
  }
  return bits;
}

static void FillCommon(bench::FastPayload& p) {
  p.test_id = kTestId;
  p.pre_ms = g_cfg.pre_delay_ms;
  p.post_ms = g_cfg.post_delay_ms;
  p.assoc_bits = g_assoc_bits;
  p.retry_max = g_cfg.retry_max;
  p.post_mode = static_cast<std::uint8_t>(g_cfg.post_mode);
}

static ae::DataBuffer MakeFullPayload() {
  bench::FastPayload p{};
  p.type = static_cast<std::uint8_t>(bench::FastMsgType::kFull);
  FillCommon(p);
  p.prepared_index = static_cast<std::uint8_t>(
      kPreparedPerVariant > 255 ? 255 : kPreparedPerVariant);
  p.sequence_global = NextSeq();
  p.cycle_us = g_have_pending_full ? g_pending_full_us : 0;
  p.connect_us = g_registration_us;
  g_have_pending_full = false;
  return bench::EncodeFast<ae::DataBuffer>(p);
}

static ae::DataBuffer MakePreparedPayload(int index) {
  bench::FastPayload p{};
  p.type = static_cast<std::uint8_t>(bench::FastMsgType::kPrepared);
  FillCommon(p);
  p.prepared_index = static_cast<std::uint8_t>(index);
  p.sequence_global = NextSeq();
  if (index > 1) {
    p.cycle_us = g_last_result.cycle_us;
    p.connect_us = g_last_result.connect_us;
    p.status_flags = g_last_result.status_flags;
    p.auth_negotiated = g_last_result.negotiated_auth;
    p.cb_any = g_last_result.cb_any;
    p.cb_match = g_last_result.cb_match;
    p.cb_count = g_last_result.cb_count;
  }
  return bench::EncodeFast<ae::DataBuffer>(p);
}

static ae::DataBuffer MakeFinalPayload() {
  bench::FastPayload p{};
  p.type = static_cast<std::uint8_t>(bench::FastMsgType::kFinal);
  FillCommon(p);
  p.prepared_index = static_cast<std::uint8_t>(
      kPreparedPerVariant > 255 ? 255 : kPreparedPerVariant);
  p.sequence_global = NextSeq();
  p.cycle_us = g_last_result.cycle_us;
  p.connect_us = g_last_result.connect_us;
  p.status_flags = g_last_result.status_flags;
  p.auth_negotiated = g_last_result.negotiated_auth;
  p.wifi_ready_count = g_wifi_ready_count;
  p.encode_count = g_encode_count;
  p.sendto_count = g_sendto_count;
  auto const left = prepared_send::PreparedMessageLeft();
  std::uint32_t consumed = 0;
  if (g_nonce_start >= left) {
    consumed = g_nonce_start - left;
  } else {
    consumed = g_encode_count;
  }
  p.nonce_consumed = consumed > 0xffffu ? 0xffffu
                                        : static_cast<std::uint16_t>(consumed);
  p.cb_any = g_last_result.cb_any;
  p.cb_match = g_last_result.cb_match;
  p.cb_count = g_last_result.cb_count;
  return bench::EncodeFast<ae::DataBuffer>(p);
}

static void ConstructAether() {
#if defined(ESP_PLATFORM)
  PreConstructCleanup();
#endif
  g_had_aether_app = true;
  g_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}
#if AE_DISTILLATION && defined(ESP_PLATFORM)
          .AddAdapterFactory([&](ae::AetherAppContext const& ctx) {
            return ae::WifiAdapter::ptr::Create(
                ae::CreateWith{ctx.domain()}.with_id(
                    ae::GlobalId::kWiFiAdapter),
                ctx.aether(), ctx.poller(), ctx.dns_resolver(), kWifiInit);
          })
#endif
  );
}

static void DoFullWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto payload = MakeFullPayload();
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    g_full_write_ok = (st == ae::WriteAction::Status::kSuccess);
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
  g_phase = Phase::kRegister;
  g_write_armed = false;
  g_pending_register_finish = false;
  g_t0 = NowUs();
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

static void StartFullCycle() {
  g_phase = Phase::kFullCycle;
  g_write_armed = false;
  g_pending_full_post_write = false;
  g_full_write_ok = false;
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_t0 = NowUs();
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

static void StartPreparedPhase() {
  g_phase = Phase::kPrepared;
  g_prepared_index = 1;
  g_prepared_waiting_gap = false;
  g_wifi_ready_count = 0;
  g_encode_count = 0;
  g_sendto_count = 0;
  g_last_result = {};
  g_nonce_start =
      static_cast<std::uint16_t>(prepared_send::PreparedMessageLeft());
#if defined(ESP_PLATFORM)
  prepared_send::ReleaseFullAetherWifiForHotPath();
  vTaskDelay(pdMS_TO_TICKS(200));
#endif
}

static void DoFinalWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto& wa = g_stream->Write(MakeFinalPayload());
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status) {
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
  g_phase = Phase::kFinal;
  g_write_armed = false;
  g_pending_final_exit = false;
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
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
  g_app->Exit(0);
}

static void FinishFullPostWriteInLoop() {
  if (!g_full_write_ok) {
    g_app->Exit(1);
    return;
  }
  if (!prepared_send::ExportPreparedSendBlock(g_client, kServiceUid,
                                              kPreparedPerVariant)) {
    g_app->Exit(1);
    return;
  }
  if (!prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() !=
          static_cast<std::uint32_t>(kPreparedPerVariant)) {
    g_app->Exit(1);
    return;
  }
  if (!prepared_send::FreezeBisectWifiCacheFromActiveConnection()) {
    g_app->Exit(1);
    return;
  }
  g_cache = prepared_send::GetBisectWifiCacheSnapshot();
  g_app->aether().Save();
  g_app->Exit(0);
}

}  // namespace
}  // namespace temp_sensor

void setup() {
  using namespace temp_sensor;
  g_cfg = MakeFastConfig();
  g_assoc_bits = AssocBitsOf(g_cfg);
#if defined(ESP_PLATFORM)
  nvs_flash_init();
  prepared_send::InvalidatePreparedWifiCache();
#endif
  g_done = false;
  g_seq = 0;
  g_registration_pending = true;
  g_pending_register_finish = false;
  g_pending_full_post_write = false;
  g_pending_final_exit = false;
}

void loop() {
  using namespace temp_sensor;
  if (g_done) {
    return;
  }

  if (g_registration_pending) {
    g_registration_pending = false;
    StartRegister();
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
      g_app->Exit(0);
      return true;
    }
    return false;
  };

  if (process_deferred()) {
    return;
  }

  if (g_phase == Phase::kPrepared) {
#if defined(ESP_PLATFORM)
    if (g_prepared_waiting_gap) {
      if (xTaskGetTickCount() < g_prepared_gap_until) {
        vTaskDelay(pdMS_TO_TICKS(20));
        return;
      }
      g_prepared_waiting_gap = false;
    }
#endif

    if (g_prepared_index > kPreparedPerVariant) {
      StartFinal();
      return;
    }

    int const i = g_prepared_index;
    auto payload = MakePreparedPayload(i);
#if defined(ESP_PLATFORM)
    auto const result =
        prepared_send::SendPreparedOnceWithFastPath(g_cfg, payload);
#else
    prepared_send::FastSendResult result{};
    result.status = prepared_send::HotSendStatus::kUnsupported;
#endif
    g_last_result = result;
    if (result.status_flags &
        static_cast<std::uint8_t>(bench::BisectStatusBits::kWifiReady)) {
      ++g_wifi_ready_count;
    }
    if (result.status_flags &
        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk)) {
      ++g_encode_count;
    }
    if (result.status_flags &
        static_cast<std::uint8_t>(bench::BisectStatusBits::kSendtoOk)) {
      ++g_sendto_count;
    }

    ++g_prepared_index;
    if (g_prepared_index <= kPreparedPerVariant) {
#if defined(ESP_PLATFORM)
      g_prepared_waiting_gap = true;
      g_prepared_gap_until =
          xTaskGetTickCount() + pdMS_TO_TICKS(kPreparedGapMs);
#endif
    }
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

  if (g_phase == Phase::kRegister) {
    ReleaseApp();
    g_registration_us = static_cast<std::uint32_t>(NowUs() - g_t0);
    StartFullCycle();
    return;
  }

  if (g_phase == Phase::kFullCycle) {
    ReleaseApp();
    auto const full_us = static_cast<std::uint32_t>(NowUs() - g_t0);
    g_pending_full_us = full_us;
    g_have_pending_full = true;
    StartPreparedPhase();
    return;
  }

  if (g_phase == Phase::kFinal) {
    ReleaseApp();
    g_phase = Phase::kDone;
    g_done = true;
  }
}
