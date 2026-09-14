/*
 * Copyright 2026 Aethernet Inc.
 *
 * No-sleep prepared-message E2E bench on ESP32-C6:
 * 1) one registration
 * 2) one FULL cycle + PrepareSendMessageBlock(10)
 * 3) ten prepared sends without AetherApp (1s gap outside timer)
 */

#include <algorithm>
#include <array>
#include <climits>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <string_view>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "prepared_send/prepared_send.h"
#include "wifi_lifecycle_out.h"

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
#  define BENCH_CLIENT_ID "prepared_message_bench_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("3d284a4f-ebb4-451e-a2c5-aecb0d647a45");
#endif

static constexpr int kPreparedCount = 10;
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
#endif

enum class Phase : std::uint8_t {
  kRegister,
  kFullCycle,
  kPrepared,
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
static int g_prepared_index = 0;  // next PREPARED sequence to send (1..10)
static bool g_prepared_waiting_gap = false;
#if defined(ESP_PLATFORM)
static TickType_t g_prepared_gap_until = 0;
#endif

static std::uint32_t g_registration_ms = 0;
static std::uint32_t g_full_cycle_ms = 0;
static std::array<std::uint32_t, kPreparedCount> g_prepared_ms{};
static int g_prepared_completed = 0;

#if defined(ESP_PLATFORM)
static std::int64_t g_t0 = 0;
#endif

static void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

static void StartRegister();
static void StartFullCycle();
static void StartPreparedPhase();
static void TickPreparedPhase();
static void PrintFinal();

static void OnRegisterReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  auto const uid_text = ae::Format("{}", g_client->uid());
  WifiLifecyclePrintf("REGISTRATION_CLIENT_UID=%s\n", uid_text.c_str());
  g_app->aether().Save();
  g_app->Exit(0);
}

static void DoFullWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto payload = prepared_send::MakeBenchPayload("FULL", 0);
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st != ae::WriteAction::Status::kSuccess) {
      WifiLifecyclePrintf("FULL_CYCLE_ERR write_fail\n");
      g_app->Exit(1);
      return;
    }

    if (!prepared_send::ExportPreparedSendBlock(g_client, kServiceUid,
                                                kPreparedCount)) {
      WifiLifecyclePrintf("FULL_CYCLE_ERR prepare_block_fail\n");
      g_app->Exit(1);
      return;
    }

    auto const left = prepared_send::PreparedMessageLeft();
    if (!prepared_send::HasPreparedSendBlock() || left != kPreparedCount) {
      WifiLifecyclePrintf(
          "FULL_CYCLE_ERR block_invalid left=%lu expected=%d\n",
          static_cast<unsigned long>(left), kPreparedCount);
      g_app->Exit(1);
      return;
    }

    WifiLifecyclePrintf("PREPARED_BLOCK reserved=%d remaining=%lu\n",
                        kPreparedCount, static_cast<unsigned long>(left));
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
#if defined(ESP_PLATFORM)
  PreConstructCleanup();
  g_t0 = NowUs();
#endif
  WifiLifecyclePrintf("REGISTRATION_START client_id=%s\n", kBenchClientId);
  WifiLifecyclePrintf("REGISTRATION_BEFORE_CONSTRUCT\n");
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
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         WifiLifecyclePrintf("REGISTRATION_ERR select_fail\n");
                         g_app->Exit(1);
                         return;
                       }
                       OnRegisterReady(std::move(res).value());
                     });
  WifiLifecyclePrintf("REGISTRATION_AFTER_CONSTRUCT\n");
}

static void StartFullCycle() {
  g_phase = Phase::kFullCycle;
  g_write_armed = false;
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
#if defined(ESP_PLATFORM)
  PreConstructCleanup();
  g_t0 = NowUs();
#endif
  WifiLifecyclePrintf("FULL_CYCLE_START\n");
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
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         WifiLifecyclePrintf("FULL_CYCLE_ERR select_fail\n");
                         g_app->Exit(1);
                         return;
                       }
                       OnFullClientReady(std::move(res).value());
                     });
}

static std::uint32_t Percentile(std::array<std::uint32_t, kPreparedCount> vals,
                                int count, int pct) {
  if (count <= 0) {
    return 0;
  }
  std::sort(vals.begin(), vals.begin() + count);
  auto const idx = (pct * (count - 1) + 99) / 100;
  return vals[static_cast<size_t>(idx)];
}

static void PrintFinal() {
  std::uint32_t min_ms = UINT32_MAX;
  std::uint32_t max_ms = 0;
  for (int i = 0; i < g_prepared_completed; ++i) {
    auto const v = g_prepared_ms[static_cast<size_t>(i)];
    min_ms = std::min(min_ms, v);
    max_ms = std::max(max_ms, v);
  }
  if (g_prepared_completed == 0) {
    min_ms = 0;
  }
  auto const median =
      Percentile(g_prepared_ms, g_prepared_completed, 50);
  auto const p90 = Percentile(g_prepared_ms, g_prepared_completed, 90);

  WifiLifecyclePrintf("REGISTRATION\n");
  WifiLifecyclePrintf("  time_ms=%lu\n",
                      static_cast<unsigned long>(g_registration_ms));
  WifiLifecyclePrintf("FULL CYCLE\n");
  WifiLifecyclePrintf("  time_ms=%lu\n",
                      static_cast<unsigned long>(g_full_cycle_ms));
  WifiLifecyclePrintf("PREPARED\n");
  WifiLifecyclePrintf("  raw=[");
  for (int i = 0; i < g_prepared_completed; ++i) {
    if (i > 0) {
      WifiLifecyclePrintf(", ");
    }
    WifiLifecyclePrintf(
        "%lu",
        static_cast<unsigned long>(g_prepared_ms[static_cast<size_t>(i)]));
  }
  WifiLifecyclePrintf("]\n");
  WifiLifecyclePrintf("  min=%lu\n", static_cast<unsigned long>(min_ms));
  WifiLifecyclePrintf("  median=%lu\n", static_cast<unsigned long>(median));
  WifiLifecyclePrintf("  p90=%lu\n", static_cast<unsigned long>(p90));
  WifiLifecyclePrintf("  max=%lu\n", static_cast<unsigned long>(max_ms));
  WifiLifecyclePrintf("  completed=%d/%d\n", g_prepared_completed,
                      kPreparedCount);
  WifiLifecyclePrintf("prepared block:\n");
  WifiLifecyclePrintf("  reserved=%d\n", kPreparedCount);
  WifiLifecyclePrintf(
      "  remaining=%lu\n",
      static_cast<unsigned long>(prepared_send::PreparedMessageLeft()));
  WifiLifecyclePrintf("PREPARED_E2E_DONE\n");
}

static void StartPreparedPhase() {
  g_phase = Phase::kPrepared;
  g_prepared_index = 1;
  g_prepared_waiting_gap = false;
#if defined(ESP_PLATFORM)
  prepared_send::ReleaseFullAetherWifiForHotPath();
#endif
  WifiLifecyclePrintf("PREPARED_LOOP_START count=%d\n", kPreparedCount);
}

static void TickPreparedPhase() {
  if (g_done || g_phase != Phase::kPrepared) {
    return;
  }

#if defined(ESP_PLATFORM)
  if (g_prepared_waiting_gap) {
    if (xTaskGetTickCount() < g_prepared_gap_until) {
      vTaskDelay(pdMS_TO_TICKS(20));
      return;
    }
    g_prepared_waiting_gap = false;
  }
#endif

  if (g_prepared_index > kPreparedCount) {
    PrintFinal();
    g_phase = Phase::kDone;
    g_done = true;
    return;
  }

  int const i = g_prepared_index;
#if defined(ESP_PLATFORM)
  auto const t0 = NowUs();
#endif
  auto payload = prepared_send::MakeBenchPayload("PREPARED", i);
  auto const status = prepared_send::SendPreparedOnce(payload);
#if defined(ESP_PLATFORM)
  auto const ms = static_cast<std::uint32_t>((NowUs() - t0) / 1000);
#else
  auto const ms = 0U;
#endif
  auto const left = prepared_send::PreparedMessageLeft();
  auto const status_text = std::string{prepared_send::ToString(status)};
  WifiLifecyclePrintf(
      "PREPARED %d time_ms=%lu status=%s message_left=%lu\n", i,
      static_cast<unsigned long>(ms), status_text.c_str(),
      static_cast<unsigned long>(left));
  if (status == prepared_send::HotSendStatus::kSent &&
      g_prepared_completed < kPreparedCount) {
    g_prepared_ms[static_cast<size_t>(g_prepared_completed)] = ms;
    ++g_prepared_completed;
  }

  ++g_prepared_index;
  if (g_prepared_index <= kPreparedCount) {
#if defined(ESP_PLATFORM)
    g_prepared_waiting_gap = true;
    g_prepared_gap_until =
        xTaskGetTickCount() + pdMS_TO_TICKS(kPreparedGapMs);
#else
    g_prepared_waiting_gap = false;
#endif
  }
}

void BeginAppMainTiming() {}
void FinalizeCycleBeforeSleep() {}
#if defined(ESP_PLATFORM)
void EnterDeepSleep() {}
#endif

void setup() {
#if defined(ESP_PLATFORM)
  nvs_flash_init();
#endif
  auto const service_text = ae::Format("{}", kServiceUid);
  WifiLifecyclePrintf("PREPARED_E2E_START client_id=%s service=%s\n",
                      kBenchClientId, service_text.c_str());
  g_done = false;
  g_prepared_index = 0;
  g_prepared_waiting_gap = false;
  g_prepared_completed = 0;
  g_registration_pending = true;
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
    TickPreparedPhase();
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
#if defined(ESP_PLATFORM)
    g_registration_ms = static_cast<std::uint32_t>((NowUs() - g_t0) / 1000);
#else
    g_registration_ms = 0;
#endif
    WifiLifecyclePrintf("REGISTRATION time_ms=%lu\n",
                        static_cast<unsigned long>(g_registration_ms));
    StartFullCycle();
    return;
  }

  if (g_phase == Phase::kFullCycle) {
    ReleaseApp();
#if defined(ESP_PLATFORM)
    g_full_cycle_ms = static_cast<std::uint32_t>((NowUs() - g_t0) / 1000);
#else
    g_full_cycle_ms = 0;
#endif
    WifiLifecyclePrintf("FULL CYCLE time_ms=%lu\n",
                        static_cast<unsigned long>(g_full_cycle_ms));
    // No further AetherApp construction — prepared hot path only.
    StartPreparedPhase();
  }
}

}  // namespace

}  // namespace temp_sensor

void setup() { temp_sensor::setup(); }
void loop() { temp_sensor::loop(); }
