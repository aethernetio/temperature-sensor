/*
 * Copyright 2026 Aethernet Inc.
 *
 * Silent single-factor Wi-Fi bisect for prepared reconnect sends (ESP32-C6).
 * Each variant differs from the same canonical baseline by exactly one factor
 * (except documented dependent combinations C3/C6/C7/C8).
 *
 * AE_EXP_BISECT_SMOKE=1  → B1 only, 2 prepared sends.
 * AE_EXP_BISECT_CONSOLE=1 → USB stage markers (no AE_EXP_SILENT).
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
#  if defined(AE_EXP_BISECT_CONSOLE)
#    include <cstdio>
#  endif
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

static constexpr int kAllVariantCount =
    static_cast<int>(prepared_send::WifiBisectVariant::kCount);

#if defined(AE_EXP_BISECT_SMOKE) && AE_EXP_BISECT_SMOKE
// Smoke: B1 only, 2 prepared reconnect sends.
static constexpr int kFirstVariant =
    static_cast<int>(prepared_send::WifiBisectVariant::kB1);
static constexpr int kLastVariantExclusive = kFirstVariant + 1;
static constexpr int kPreparedPerVariant = 2;
#else
static constexpr int kFirstVariant = 0;
static constexpr int kLastVariantExclusive = kAllVariantCount;
static constexpr int kPreparedPerVariant = 20;
#endif

static constexpr int kPreparedGapMs = 1000;

#if defined(ESP_PLATFORM) && defined(AE_EXP_BISECT_CONSOLE)
static void Stage(char const* msg) {
  std::printf("%s\n", msg);
  std::fflush(stdout);
}
static void Stage2(char const* a, char const* b) {
  std::printf("%s %s\n", a, b);
  std::fflush(stdout);
}
#else
static void Stage(char const*) {}
static void Stage2(char const*, char const*) {}
#endif

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

// Deferred work: never Prepare/Freeze/Save/Exit inside Write callbacks.
static bool g_pending_register_finish = false;
static bool g_pending_full_post_write = false;
static bool g_full_write_ok = false;
static bool g_pending_final_exit = false;

static int g_variant = kFirstVariant;
static int g_prepared_index = 0;
static bool g_prepared_waiting_gap = false;
#if defined(ESP_PLATFORM)
static TickType_t g_prepared_gap_until = 0;
#endif

static std::uint16_t g_seq = 0;
static std::uint32_t g_registration_us = 0;
static std::uint32_t g_last_full_us = 0;
static std::uint32_t g_last_prepared_us = 0;
static std::uint32_t g_pending_full_us = 0;
static bool g_have_pending_full = false;

static std::uint8_t g_wifi_ready_count = 0;
static std::uint8_t g_encode_count = 0;
static std::uint8_t g_sendto_count = 0;
static std::uint8_t g_nonce_start = 0;

static prepared_send::BisectSendResult g_last_result{};
static prepared_send::BisectWifiCacheSnapshot g_cache{};

static std::uint8_t g_prev_wifi_ready = 0;
static std::uint8_t g_prev_encode = 0;
static std::uint8_t g_prev_sendto = 0;
static std::uint8_t g_prev_nonce = 0;
static bool g_have_prev_summary = false;

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

static prepared_send::WifiBisectVariant CurrentVariant() {
  return static_cast<prepared_send::WifiBisectVariant>(g_variant);
}

static char const* CurrentVariantName() {
  return bench::BisectVariantName(static_cast<std::uint8_t>(g_variant));
}

static ae::DataBuffer MakeFullPayload() {
  bench::BisectPayload p{};
  p.type = static_cast<std::uint8_t>(bench::BisectMsgType::kFull);
  p.variant_id = static_cast<std::uint8_t>(g_variant);
  p.sequence_global = NextSeq();
  p.time_us = g_have_pending_full ? g_pending_full_us : 0;
  p.aux_us = (g_variant == kFirstVariant) ? g_registration_us : 0;
  if (g_have_prev_summary) {
    p.wifi_ready_count = g_prev_wifi_ready;
    p.encode_count = g_prev_encode;
    p.sendto_count = g_prev_sendto;
    p.nonce_consumed = g_prev_nonce;
  }
  p.cached_ip = g_cache.ip;
  p.cached_channel = g_cache.channel;
  p.requested_channel = g_cache.channel;
  p.actual_channel = g_cache.channel;
  std::memcpy(p.cached_bssid, g_cache.bssid, sizeof(p.cached_bssid));
  p.pre_delay_ms =
      (CurrentVariant() == prepared_send::WifiBisectVariant::kB0) ? 0 : 200;
  g_have_pending_full = false;
  return bench::EncodeBisect<ae::DataBuffer>(p);
}

static ae::DataBuffer MakePreparedPayload(int index) {
  bench::BisectPayload p{};
  p.type = static_cast<std::uint8_t>(bench::BisectMsgType::kPrepared);
  p.variant_id = static_cast<std::uint8_t>(g_variant);
  p.prepared_index = static_cast<std::uint8_t>(index);
  p.sequence_global = NextSeq();
  if (index == 1) {
    p.cached_ip = g_cache.ip;
    p.cached_channel = g_cache.channel;
    std::memcpy(p.cached_bssid, g_cache.bssid, sizeof(p.cached_bssid));
    p.pre_delay_ms =
        (CurrentVariant() == prepared_send::WifiBisectVariant::kB0) ? 0 : 200;
  } else {
    p.time_us = g_last_prepared_us;
    p.requested_channel = g_last_result.requested_channel;
    p.actual_channel = g_last_result.actual_channel;
    p.status_flags = g_last_result.status_flags;
    p.factor_bits = g_last_result.factor_bits;
    p.pre_delay_ms = g_last_result.pre_delay_ms;
  }
  return bench::EncodeBisect<ae::DataBuffer>(p);
}

static ae::DataBuffer MakeFinalPayload() {
  bench::BisectPayload p{};
  p.type = static_cast<std::uint8_t>(bench::BisectMsgType::kFinal);
  p.variant_id = static_cast<std::uint8_t>(g_variant > 0 ? g_variant - 1
                                                         : kFirstVariant);
  p.prepared_index = kPreparedPerVariant;
  p.sequence_global = NextSeq();
  p.time_us = g_last_prepared_us;
  p.aux_us = g_registration_us;
  p.requested_channel = g_last_result.requested_channel;
  p.actual_channel = g_last_result.actual_channel;
  p.status_flags = g_last_result.status_flags;
  p.factor_bits = g_last_result.factor_bits;
  p.pre_delay_ms = g_last_result.pre_delay_ms;
  p.wifi_ready_count = g_wifi_ready_count;
  p.encode_count = g_encode_count;
  p.sendto_count = g_sendto_count;
  auto const left = prepared_send::PreparedMessageLeft();
  p.nonce_consumed = g_nonce_start >= left
                         ? static_cast<std::uint8_t>(g_nonce_start - left)
                         : g_encode_count;
  return bench::EncodeBisect<ae::DataBuffer>(p);
}

static void ConstructAether() {
  Stage("CONSTRUCT_BEGIN");
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
  Stage("CONSTRUCT_DONE");
}

static void DoFullWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  Stage("FULL_WRITE_BEGIN");
  auto payload = MakeFullPayload();
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    // Defer Prepare/Freeze/Save/Exit into the main loop — never nest them
    // inside the Write completion path.
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
  Stage("STREAM_WRITABLE");
  DoFullWrite();
}

static void OnFullClientReady(ae::Client::ptr client_ptr) {
  Stage("SELECT_DONE");
  g_client = std::move(client_ptr);
  Stage("STREAM_BEGIN");
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
  Stage("SELECT_BEGIN");
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         Stage("SELECT_FAIL");
                         g_app->Exit(1);
                         return;
                       }
                       Stage("SELECT_RESULT");
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
  Stage2("BISECT_VARIANT_BEGIN", CurrentVariantName());
  ConstructAether();
  Stage("SELECT_BEGIN");
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         Stage("SELECT_FAIL");
                         g_app->Exit(1);
                         return;
                       }
                       Stage("SELECT_RESULT");
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
  g_last_prepared_us = 0;
  g_last_result = {};
  g_nonce_start =
      static_cast<std::uint8_t>(prepared_send::PreparedMessageLeft());
  Stage("RELEASE_BEGIN");
#if defined(ESP_PLATFORM)
  prepared_send::ReleaseFullAetherWifiForHotPath();
  // Let IDF finish tearing down before the first bisect STA init.
  vTaskDelay(pdMS_TO_TICKS(200));
#endif
  Stage("RELEASE_DONE");
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

static void StorePrevSummaryAndAdvance() {
  Stage2("BISECT_VARIANT_DONE", CurrentVariantName());
  g_prev_wifi_ready = g_wifi_ready_count;
  g_prev_encode = g_encode_count;
  g_prev_sendto = g_sendto_count;
  auto const left = prepared_send::PreparedMessageLeft();
  g_prev_nonce = g_nonce_start >= left
                     ? static_cast<std::uint8_t>(g_nonce_start - left)
                     : g_encode_count;
  g_have_prev_summary = true;
  ++g_variant;
  if (g_variant >= kLastVariantExclusive) {
    StartFinal();
  } else {
    StartFullCycle();
  }
}

static void FinishRegisterInLoop() {
  Stage("SELECT_DONE");
  Stage("SAVE_BEGIN");
  g_app->aether().Save();
  Stage("SAVE_DONE");
  g_app->Exit(0);
}

static void FinishFullPostWriteInLoop() {
  Stage("FULL_WRITE_DONE");
  if (!g_full_write_ok) {
    g_app->Exit(1);
    return;
  }
  Stage("PREPARE_BLOCK_BEGIN");
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
  Stage("PREPARE_BLOCK_DONE");
  if (!prepared_send::FreezeBisectWifiCacheFromActiveConnection()) {
    g_app->Exit(1);
    return;
  }
  g_cache = prepared_send::GetBisectWifiCacheSnapshot();
  Stage("SAVE_BEGIN");
  g_app->aether().Save();
  Stage("SAVE_DONE");
  g_app->Exit(0);
}

void setup() {
  Stage("BOOT");
#if defined(ESP_PLATFORM)
  nvs_flash_init();
  prepared_send::InvalidatePreparedWifiCache();
#endif
  g_done = false;
  g_seq = 0;
  g_registration_pending = true;
  g_variant = kFirstVariant;
  g_have_prev_summary = false;
  g_pending_register_finish = false;
  g_pending_full_post_write = false;
  g_pending_final_exit = false;
}

void loop() {
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

  // Catch deferred work that was armed before this tick.
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
      StorePrevSummaryAndAdvance();
      return;
    }

    int const i = g_prepared_index;
    auto payload = MakePreparedPayload(i);
#if defined(ESP_PLATFORM)
    auto const result = prepared_send::SendPreparedOnceWithBisectFactor(
        CurrentVariant(), payload);
#else
    prepared_send::BisectSendResult result{};
    result.status = prepared_send::HotSendStatus::kUnsupported;
#endif
    g_last_result = result;
    g_last_prepared_us = result.total_us;
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
    // SelectClient / Write may arm deferred work during Update — run it before
    // WaitUntil so we do not stall on the next wake delay.
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
    g_last_full_us = full_us;
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

}  // namespace
}  // namespace temp_sensor

void setup() { temp_sensor::setup(); }
void loop() { temp_sensor::loop(); }
