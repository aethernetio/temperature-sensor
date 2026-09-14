/*
 * Copyright 2026 Aethernet Inc.
 *
 * Silent 5x20 prepared Wi-Fi cache experiment (ESP32-C6):
 * 1 registration, 5 FULL cycles each preparing 20 messages, 100 prepared sends.
 * All timings travel in binary application payloads (no UART results).
 */

#include <cstdint>
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
#  define BENCH_CLIENT_ID "prepared_wifi_cache_5x20_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("3d284a4f-ebb4-451e-a2c5-aecb0d647a45");
#endif

static constexpr int kOuterCycles = 5;
static constexpr int kPreparedPerCycle = 20;
static constexpr int kPreparedGapMs = 1000;

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

static int g_outer = 0;
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
static std::uint8_t g_sticky_cache_flags = 0;

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

static ae::DataBuffer MakeFullPayload(int outer) {
  bench::Payload p{};
  p.type = static_cast<std::uint8_t>(bench::MsgType::kFull);
  p.outer_cycle = static_cast<std::uint8_t>(outer);
  p.prepared_index = 0;
  p.sequence_global = NextSeq();
  p.registration_us = (outer == 1) ? g_registration_us : 0;
  p.previous_full_us = g_have_pending_full ? g_pending_full_us : 0;
  p.previous_prepared_us = g_last_prepared_us;
  p.cache_flags = g_sticky_cache_flags;
  g_have_pending_full = false;
  return bench::Encode<ae::DataBuffer>(p);
}

static ae::DataBuffer MakePreparedPayload(int outer, int index) {
  bench::Payload p{};
  p.type = static_cast<std::uint8_t>(bench::MsgType::kPrepared);
  p.outer_cycle = static_cast<std::uint8_t>(outer);
  p.prepared_index = static_cast<std::uint8_t>(index);
  p.sequence_global = NextSeq();
  p.previous_prepared_us = (index == 1) ? 0 : g_last_prepared_us;
  // cache_flags describe the *previous* prepared Wi-Fi attempt (index-1).
  p.cache_flags = (index == 1) ? 0 : g_sticky_cache_flags;
  return bench::Encode<ae::DataBuffer>(p);
}

static ae::DataBuffer MakeFinalPayload() {
  bench::Payload p{};
  p.type = static_cast<std::uint8_t>(bench::MsgType::kFinal);
  p.outer_cycle = kOuterCycles;
  p.prepared_index = kPreparedPerCycle;
  p.sequence_global = NextSeq();
  p.registration_us = g_registration_us;
  p.previous_full_us = g_have_pending_full ? g_pending_full_us : g_last_full_us;
  p.previous_prepared_us = g_last_prepared_us;
  p.cache_flags = g_sticky_cache_flags;
  return bench::Encode<ae::DataBuffer>(p);
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

static void OnRegisterReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  g_app->aether().Save();
  g_app->Exit(0);
}

static void DoFullWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto payload = MakeFullPayload(g_outer);
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st != ae::WriteAction::Status::kSuccess) {
      g_app->Exit(1);
      return;
    }
    if (!prepared_send::ExportPreparedSendBlock(g_client, kServiceUid,
                                                kPreparedPerCycle)) {
      g_app->Exit(1);
      return;
    }
    if (!prepared_send::HasPreparedSendBlock() ||
        prepared_send::PreparedMessageLeft() !=
            static_cast<std::uint32_t>(kPreparedPerCycle)) {
      g_app->Exit(1);
      return;
    }
    // Export association/IP/gateway-MAC into local prepared cache while FULL
    // Wi-Fi is still up so prepared #1 can take the fast path.
    (void)prepared_send::CapturePreparedWifiCacheFromActiveConnection();
    g_app->aether().Save();
    g_app->Exit(0);
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
                       OnRegisterReady(std::move(res).value());
                     });
}

static void StartFullCycle(int outer) {
  g_phase = Phase::kFullCycle;
  g_outer = outer;
  g_write_armed = false;
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
  g_sticky_cache_flags = 0;
#if defined(ESP_PLATFORM)
  prepared_send::ReleaseFullAetherWifiForHotPath();
#endif
}

static void DoFinalWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto payload = MakeFinalPayload();
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub =
      wa.status_event().Subscribe([](ae::WriteAction::Status) { g_app->Exit(0); });
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

void BeginAppMainTiming() {}
void FinalizeCycleBeforeSleep() {}
#if defined(ESP_PLATFORM)
void EnterDeepSleep() {}
#endif

void setup() {
#if defined(ESP_PLATFORM)
  nvs_flash_init();
  prepared_send::InvalidatePreparedWifiCache();
#endif
  g_done = false;
  g_seq = 0;
  g_registration_pending = true;
  g_last_prepared_us = 0;
  g_have_pending_full = false;
  g_sticky_cache_flags = 0;
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

    if (g_prepared_index > kPreparedPerCycle) {
      if (g_outer < kOuterCycles) {
        StartFullCycle(g_outer + 1);
      } else {
        StartFinal();
      }
      return;
    }

    int const i = g_prepared_index;
    auto payload = MakePreparedPayload(g_outer, i);
    auto const t0 = NowUs();
    auto const status = prepared_send::SendPreparedOnce(payload);
    auto const us = static_cast<std::uint32_t>(NowUs() - t0);
    (void)status;
    g_last_prepared_us = us;
    g_sticky_cache_flags = prepared_send::LastSendCacheFlags();

    ++g_prepared_index;
    if (g_prepared_index <= kPreparedPerCycle) {
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
    g_app->WaitUntil(t);
    return;
  }

  if (g_phase == Phase::kRegister) {
    ReleaseApp();
    g_registration_us = static_cast<std::uint32_t>(NowUs() - g_t0);
    StartFullCycle(1);
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
