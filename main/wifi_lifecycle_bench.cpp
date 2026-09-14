/*
 * Copyright 2026 Aethernet Inc.
 *
 * Wi-Fi lifecycle benchmark: single metric init_to_release_ms, 10 cycles.
 */
#include <algorithm>
#include <array>
#include <climits>
#include <cstdint>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "aether-miscpp/serialization/binary_archive.h"
#include "wifi_lifecycle_out.h"

#if defined(ESP_PLATFORM)
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_timer.h>
#  include <esp_wifi.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#endif

using namespace std::chrono_literals;

namespace temp_sensor {
namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kClientId = "Controller";

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("015f10b0-73cc-4917-8804-5ecb19ede984");
#endif

#if defined(AE_EXP_WIFI_LIFECYCLE_CYCLES)
static constexpr int kMeasureCycles = AE_EXP_WIFI_LIFECYCLE_CYCLES;
#else
static constexpr int kMeasureCycles = 10;
#endif

#if defined(AE_EXP_WIFI_COOLDOWN_MS)
static constexpr int kCooldownMs = AE_EXP_WIFI_COOLDOWN_MS;
#else
static constexpr int kCooldownMs = 0;
#endif

#if defined(AE_EXP_WIFI_LIFECYCLE_VARIANT)
static constexpr int kVariant = AE_EXP_WIFI_LIFECYCLE_VARIANT;
#else
static constexpr int kVariant = 0;
#endif

#if defined(ESP_PLATFORM)
static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

static void PreConstructCleanup() {
  if (kCooldownMs > 0) {
    vTaskDelay(pdMS_TO_TICKS(kCooldownMs));
  }
#  if !AE_WIFI_USE_FULL_DEINIT
  // Legacy driver Deinit leaves default event loop / netif; clear before next
  // Construct so the stack can be recreated.
  esp_netif_deinit();
  esp_event_loop_delete_default();
#  endif
}

static std::int64_t NowUs() { return esp_timer_get_time(); }
#endif

struct Ping {
  AE_REFLECT_MEMBERS(root_code, size, dev_code, index)
  std::uint8_t root_code{0x3};
  std::uint8_t size{4};
  std::uint8_t dev_code{0x15};
  std::uint32_t index{0};
};

enum class Phase : std::uint8_t { kRegister, kMeasure, kDone };

static std::shared_ptr<ae::AetherApp> g_app;
static ae::Client::ptr g_client;
static std::unique_ptr<ae::P2pStream> g_stream;
static ae::Subscription g_select_sub;
static ae::Subscription g_stream_sub;
static ae::Subscription g_write_sub;

static Phase g_phase = Phase::kRegister;
static int g_cycle = 0;
static bool g_write_armed = false;
static bool g_done = false;

static std::array<std::uint32_t, kMeasureCycles> g_times_ms{};
static int g_completed = 0;

#if defined(ESP_PLATFORM)
static std::int64_t g_t0 = 0;
#endif

static ae::DataBuffer MakePing(std::uint32_t index) {
  Ping ping{};
  ping.index = index;
  ae::DataBuffer msg{};
  auto archive = ae::seri::BinaryArchive{ae::seri::BinaryVectorBuffer<>{msg}};
  archive.Save(ping);
  return msg;
}

static void ReleaseMeasuredApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

static void FinishCycleAndNext();
static void StartMeasureCycle(int cycle);

static void DoWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  auto& wa = g_stream->Write(MakePing(static_cast<std::uint32_t>(g_cycle)));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st != ae::WriteAction::Status::kSuccess) {
      WifiLifecyclePrintf("cycle=%d init_to_release_ms=0 result=FAIL\n", g_cycle);
      WifiLifecyclePrintf("WIFI_CYCLE_ERR\tcycle=%d\twrite_fail\n", g_cycle);
      g_app->Exit(1);
      return;
    }
    g_app->aether().Save();
    g_app->Exit(0);
  });
}

static void MaybeWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  DoWrite();
}

static void OnClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  if (g_phase == Phase::kRegister) {
    g_app->aether().Save();
    g_app->Exit(0);
    return;
  }

  auto client = g_client.Load();
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             ae::P2pPortHandle{});
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeWrite(); });
  MaybeWrite();
}

static void StartMeasureCycle(int cycle) {
  g_phase = Phase::kMeasure;
  g_cycle = cycle;
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
                     ->SelectClient(kParentUid, kClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         WifiLifecyclePrintf(
                             "cycle=%d init_to_release_ms=0 result=FAIL\n",
                             g_cycle);
                         WifiLifecyclePrintf("WIFI_CYCLE_ERR\tcycle=%d\tselect\n",
                                             g_cycle);
                         g_app->Exit(1);
                         return;
                       }
                       OnClientReady(std::move(res).value());
                     });
}

static void StartRegisterCycle() {
  g_phase = Phase::kRegister;
  g_cycle = 0;
  g_write_armed = false;
#if defined(ESP_PLATFORM)
  PreConstructCleanup();
#endif
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
                     ->SelectClient(kParentUid, kClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       OnClientReady(std::move(res).value());
                     });
}

static std::uint32_t MedianMs() {
  std::array<std::uint32_t, kMeasureCycles> sorted{};
  for (int i = 0; i < g_completed; ++i) {
    sorted[static_cast<size_t>(i)] = g_times_ms[static_cast<size_t>(i)];
  }
  std::sort(sorted.begin(), sorted.begin() + g_completed);
  if (g_completed == 0) {
    return 0;
  }
  if (g_completed % 2 == 1) {
    return sorted[static_cast<size_t>(g_completed / 2)];
  }
  auto const a = sorted[static_cast<size_t>(g_completed / 2 - 1)];
  auto const b = sorted[static_cast<size_t>(g_completed / 2)];
  return (a + b) / 2;
}

static void PrintSummary() {
  std::uint32_t min_ms = UINT32_MAX;
  std::uint32_t max_ms = 0;
  for (int i = 0; i < g_completed; ++i) {
    auto const v = g_times_ms[static_cast<size_t>(i)];
    min_ms = std::min(min_ms, v);
    max_ms = std::max(max_ms, v);
  }
  if (g_completed == 0) {
    min_ms = 0;
  }
  WifiLifecyclePrintf("WIFI_SUMMARY\tvariant=%d\tcompleted=%d\tmin=%lu\tmedian=%lu\tmax=%lu\n",
                      kVariant, g_completed, static_cast<unsigned long>(min_ms),
                      static_cast<unsigned long>(MedianMs()),
                      static_cast<unsigned long>(max_ms));
  WifiLifecyclePrintf("raw=");
  for (int i = 0; i < g_completed; ++i) {
    if (i > 0) {
      WifiLifecyclePrintf(",");
    }
    WifiLifecyclePrintf(
        "%lu",
        static_cast<unsigned long>(g_times_ms[static_cast<size_t>(i)]));
  }
  WifiLifecyclePrintf("\n");
  WifiLifecyclePrintf("min=%lu\n", static_cast<unsigned long>(min_ms));
  WifiLifecyclePrintf("median=%lu\n",
                      static_cast<unsigned long>(MedianMs()));
  WifiLifecyclePrintf("max=%lu\n", static_cast<unsigned long>(max_ms));
  WifiLifecyclePrintf("completed=%d\n", g_completed);
  for (int i = 0; i < g_completed; ++i) {
    WifiLifecyclePrintf("WIFI_RAW\tvariant=%d\tcycle=%d\tinit_to_release_ms=%lu\n",
                        kVariant, i + 1,
                        static_cast<unsigned long>(
                            g_times_ms[static_cast<size_t>(i)]));
  }
}

static void FinishCycleAndNext() {
  if (g_phase == Phase::kRegister) {
    ReleaseMeasuredApp();
    StartMeasureCycle(1);
    return;
  }

  ReleaseMeasuredApp();

#if defined(ESP_PLATFORM)
  auto const ms = static_cast<std::uint32_t>((NowUs() - g_t0) / 1000);
#else
  auto const ms = 0U;
#endif

  if (g_completed < kMeasureCycles) {
    g_times_ms[static_cast<size_t>(g_completed)] = ms;
    WifiLifecyclePrintf("cycle=%d init_to_release_ms=%lu result=OK\n", g_cycle,
                        static_cast<unsigned long>(ms));
    WifiLifecyclePrintf("WIFI_CYCLE\tvariant=%d\tcycle=%d\tinit_to_release_ms=%lu\n",
                        kVariant, g_cycle, static_cast<unsigned long>(ms));
    ++g_completed;
  }

  if (g_cycle < kMeasureCycles) {
    StartMeasureCycle(g_cycle + 1);
    return;
  }

  PrintSummary();
  g_done = true;
  g_phase = Phase::kDone;
}

}  // namespace

void BeginAppMainTiming() {}
void FinalizeCycleBeforeSleep() {}
#if defined(ESP_PLATFORM)
void EnterDeepSleep() {}
#endif

void setup() {
#if defined(ESP_PLATFORM)
  nvs_flash_init();
#endif
  WifiLifecyclePrintf("WIFI_BENCH_START\tvariant=%d\tcooldown_ms=%d\n", kVariant,
                      kCooldownMs);
  g_done = false;
  g_completed = 0;
  StartRegisterCycle();
}

void loop() {
  if (g_done || !g_app) {
    return;
  }
  if (!g_app->IsExited()) {
    auto t = g_app->Update(ae::Now());
    g_app->WaitUntil(t);
    return;
  }
  FinishCycleAndNext();
}

}  // namespace temp_sensor

void setup() { temp_sensor::setup(); }
void loop() { temp_sensor::loop(); }
