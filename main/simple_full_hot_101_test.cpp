/*
 * AE_EXP_SIMPLE_FULL_HOT_101: one scenario only.
 * Message 1 — FULL Æther + FULL_WIFI=vanilla (scan, DHCP, WIFI_PS_NONE).
 * Messages 2…101 — prepared HOT + HOT_WIFI=cached channel/static IP/static ARP.
 * Payload body: ASCII message number only. Deep sleep 2 s between cycles.
 */

#include <cstdio>
#include <memory>
#include <string>

#include "aether/adapters/wifi_adapter.h"
#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "prepared_send/prepared_send.h"

#include <esp_event.h>
#include <esp_sleep.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>

namespace temp_sensor {
namespace {

using temp_sensor::prepared_send::PreparedWifiRtcCache;

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);

static constexpr int kHotCount = 100;
static constexpr int kLastMessage = 1 + kHotCount;
static constexpr std::uint64_t kSleepUs = 2'000'000;
static constexpr std::uint16_t kHalted = 0xffffu;

RTC_DATA_ATTR static std::uint16_t g_next_message = 1;
RTC_DATA_ATTR static PreparedWifiRtcCache g_wifi_cache{};

static std::shared_ptr<ae::AetherApp> g_app;
static ae::Client::ptr g_client;
static std::unique_ptr<ae::P2pStream> g_stream;
static ae::Subscription g_select_sub;
static ae::Subscription g_stream_sub;
static ae::Subscription g_write_sub;
static bool g_write_started = false;
static bool g_write_ok = false;

static ae::WiFiInit VanillaWifi() {
  ae::WiFiPowerSaveParam ps{};
  ps.wifi_ps_type = WIFI_PS_NONE;
  return ae::WiFiInit{
      std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
      ps,
  };
}

static ae::DataBuffer Payload(int n) {
  std::string s = std::to_string(n);
  return ae::DataBuffer{s.begin(), s.end()};
}

static temp_sensor::prepared_send::FastPathConfig CachedWifi() {
  temp_sensor::prepared_send::FastPathConfig c{};
  c.use_channel = true;
  c.use_static_ip = true;
  c.use_static_arp = true;
  c.use_bssid = false;
  c.use_fast_scan = false;
  c.auth = temp_sensor::prepared_send::FastAuthMode::kWpa2;
  c.retry_max = 10;
  c.post_mode = temp_sensor::prepared_send::FastPostMode::kTxDoneCb;
  return c;
}

[[noreturn]] static void DeepSleep2s() {
  esp_sleep_enable_timer_wakeup(kSleepUs);
  esp_deep_sleep_start();
}

[[noreturn]] static void HaltScenario() {
  g_next_message = kHalted;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

static void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

static void ConstructVanillaAether() {
  g_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}.AddAdapterFactory(
          [&](ae::AetherAppContext const& ctx) {
            return ae::WifiAdapter::ptr::Create(
                ae::CreateWith{ctx.domain()}.with_id(ae::GlobalId::kWiFiAdapter),
                ctx.aether(), ctx.poller(), ctx.dns_resolver(), VanillaWifi());
          }));
}

static void FinishFullPath() {
  if (!g_write_ok) {
    HaltScenario();
  }
  if (!temp_sensor::prepared_send::CapturePreparedWifiRtcCache(&g_wifi_cache)) {
    HaltScenario();
  }
  if (!temp_sensor::prepared_send::ExportPreparedSendBlock(g_client, kServiceUid,
                                                           kHotCount)) {
    HaltScenario();
  }
  if (!temp_sensor::prepared_send::HasPreparedSendBlock() ||
      temp_sensor::prepared_send::PreparedMessageLeft() !=
          static_cast<std::uint32_t>(kHotCount)) {
    HaltScenario();
  }
  g_app->aether().Save();
  ReleaseApp();
  temp_sensor::prepared_send::ReleaseFullAetherWifiForHotPath();
  g_next_message = 2;
  DeepSleep2s();
}

static void TryFullWrite() {
  if (!g_stream || g_write_started) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  g_write_started = true;
  auto& wa = g_stream->Write(Payload(1));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st == ae::WriteAction::Status::kSuccess) {
      g_write_ok = true;
      return;
    }
    if (st == ae::WriteAction::Status::kFail ||
        st == ae::WriteAction::Status::kStop) {
      HaltScenario();
    }
  });
}

static void OnClientReady(ae::Client::ptr client) {
  g_client = std::move(client);
  auto loaded = g_client.Load();
  auto handle = loaded->message_stream_manager().CreatePort(kServiceUid);
  g_stream = std::make_unique<ae::P2pStream>(*g_app, loaded, kServiceUid,
                                               std::move(handle));
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { TryFullWrite(); });
  TryFullWrite();
}

static void StartFullMessage() {
  ConstructVanillaAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         HaltScenario();
                       }
                       OnClientReady(std::move(res).value());
                     });
}

static void SendHotMessage() {
  if (!temp_sensor::prepared_send::HasPreparedSendBlock() ||
      temp_sensor::prepared_send::PreparedMessageLeft() == 0) {
    HaltScenario();
  }
  auto snap =
      temp_sensor::prepared_send::SnapshotFromPreparedWifiRtcCache(g_wifi_cache);
  auto const result = temp_sensor::prepared_send::SendPreparedOnceWithFastPath(
      CachedWifi(), Payload(g_next_message), &snap);
  if (result.status != temp_sensor::prepared_send::HotSendStatus::kSent) {
    HaltScenario();
  }
  ++g_next_message;
  if (g_next_message > kLastMessage) {
    for (;;) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }
  DeepSleep2s();
}

static void ColdBootUsbWindow() {
  auto const reset = esp_reset_reason();
  if (reset == ESP_RST_POWERON || reset == ESP_RST_BROWNOUT) {
    vTaskDelay(pdMS_TO_TICKS(60000));
  }
}

void AppSetup() {
  nvs_flash_init();
  esp_event_loop_delete_default();
  std::printf("FULL_WIFI=vanilla\n");
  std::printf("HOT_WIFI=cached_channel_static_ip_static_arp\n");
  std::fflush(stdout);
  if (g_next_message == kHalted) {
    HaltScenario();
  }
  ColdBootUsbWindow();
  if (g_next_message == 1) {
    StartFullMessage();
    return;
  }
  SendHotMessage();
}

void AppLoop() {
  if (g_next_message != 1 || !g_app) {
    return;
  }
  g_app->Update(ae::Now());
  if (g_write_ok) {
    FinishFullPath();
  }
}

}  // namespace
}  // namespace temp_sensor

void setup() { temp_sensor::AppSetup(); }

void loop() { temp_sensor::AppLoop(); }
