/*

 * AE_EXP_SIMPLE_FULL_HOT_101: one scenario only.

 * Message 1 — FULL Æther + FULL_WIFI=vanilla (scan, DHCP, WIFI_PS_NONE).

 * Messages 2…101 — prepared HOT + HOT_WIFI=cached channel/static IP/static ARP.

 * Payload body: ASCII message number only. Deep sleep 2 s between cycles.

 */



#include <cstdio>

#include <cstring>

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

#include <esp_task_wdt.h>

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

static bool g_scenario_failed = false;

static char g_fail_stage[48] = {};

static char g_fail_error[64] = {};

static bool g_stream_writable_logged = false;
static ae::Client::ptr g_pending_client;
static bool g_select_ready_pending = false;



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



static char const* WriteStatusName(ae::WriteAction::Status st) {

  switch (st) {

    case ae::WriteAction::Status::kSuccess:

      return "Success";

    case ae::WriteAction::Status::kFail:

      return "Fail";

    case ae::WriteAction::Status::kStop:

      return "Stop";

  }

  return "Unknown";

}



static void MarkScenarioFail(char const* stage, char const* error) {

  if (g_scenario_failed) {

    return;

  }

  g_scenario_failed = true;

  std::snprintf(g_fail_stage, sizeof(g_fail_stage), "%s", stage);

  std::snprintf(g_fail_error, sizeof(g_fail_error), "%s", error ? error : "");

  g_next_message = kHalted;

  std::printf("SCENARIO_FAIL stage=%s error=%s\n", g_fail_stage, g_fail_error);

  std::fflush(stdout);

}



[[noreturn]] static void DeepSleep2s() {

  std::printf("SLEEP_ENTER\n");

  std::fflush(stdout);

  esp_sleep_enable_timer_wakeup(kSleepUs);

  esp_deep_sleep_start();

}



[[noreturn]] static void ScenarioFailIdle() {

  for (;;) {

    vTaskDelay(pdMS_TO_TICKS(1000));

    esp_task_wdt_reset();

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
      ae::AetherAppContext{}
#if AE_DISTILLATION
          .AdaptersFactory([&](ae::AetherAppContext const& ctx) {
            auto ap = ae::AdapterRegistry::ptr{ctx.aether()->adapter_registry};
            if (!ap.is_valid()) {
              ap = ae::AdapterRegistry::ptr::Create(
                  ae::CreateWith{ctx.domain()}
                      .with_id(ae::GlobalId::kAdapterRegistry)
                      .with_flags(ae::ObjFlags::kUnloadedByDefault));
            }
            auto loaded_reg = ap.Load();
            bool have_wifi = false;
            for (auto const& adapter : loaded_reg->adapters()) {
              if (ae::WifiAdapter::ptr{adapter}.is_valid()) {
                have_wifi = true;
                break;
              }
            }
            if (!have_wifi) {
              loaded_reg->Add(ae::WifiAdapter::ptr::Create(
                  ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
                  ctx.dns_resolver(), VanillaWifi()));
            }
            return ap;
          })
#endif
  );
}



static void FinishFullPath() {

  if (!g_write_ok) {

    MarkScenarioFail("full_write", "not_success");

    return;

  }

  bool cache_ok =

      temp_sensor::prepared_send::CapturePreparedWifiRtcCache(&g_wifi_cache);

  std::printf("WIFI_CACHE_RESULT=%s\n", cache_ok ? "ok" : "fail");

  std::fflush(stdout);

  if (!cache_ok) {

    MarkScenarioFail("wifi_cache", "capture_failed");

    return;

  }

  bool export_ok = temp_sensor::prepared_send::ExportPreparedSendBlock(

      g_client, kServiceUid, kHotCount);

  std::uint32_t remaining = temp_sensor::prepared_send::PreparedMessageLeft();

  std::printf("PREPARED_EXPORT_RESULT=%s remaining=%u\n",

              export_ok ? "ok" : "fail", static_cast<unsigned>(remaining));

  std::fflush(stdout);

  if (!export_ok) {

    MarkScenarioFail("prepared_export", "export_failed");

    return;

  }

  if (!temp_sensor::prepared_send::HasPreparedSendBlock() ||

      remaining != static_cast<std::uint32_t>(kHotCount)) {

    MarkScenarioFail("prepared_export", "remaining_mismatch");

    return;

  }

  g_app->aether().Save();

  ReleaseApp();

  temp_sensor::prepared_send::ReleaseFullAetherWifiForHotPath();

  g_next_message = 2;

  DeepSleep2s();

}



static void TryFullWrite() {

  if (!g_stream || g_write_started || g_scenario_failed) {

    return;

  }

  bool writable = g_stream->stream_info().is_writable;

  if (!g_stream_writable_logged) {

    std::printf("STREAM_STATE writable=%d\n", writable ? 1 : 0);

    std::fflush(stdout);

    g_stream_writable_logged = true;

  }

  if (!writable) {

    return;

  }

  g_write_started = true;

  std::printf("FULL_WRITE_BEGIN\n");

  std::fflush(stdout);

  auto& wa = g_stream->Write(Payload(1));

  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {

    std::printf("FULL_WRITE_STATUS=%s\n", WriteStatusName(st));

    std::fflush(stdout);

    if (st == ae::WriteAction::Status::kSuccess) {

      g_write_ok = true;

      return;

    }

    if (st == ae::WriteAction::Status::kFail ||

        st == ae::WriteAction::Status::kStop) {

      MarkScenarioFail("full_write", WriteStatusName(st));

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

  std::printf("FULL_BEGIN service_uid=%s\n", SERVICE_UID);

  std::fflush(stdout);

  ConstructVanillaAether();

  g_select_sub = g_app->aether()

                     ->SelectClient(kParentUid, kBenchClientId)

                     .result_event()

                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {

                       if (!res) {

                         std::printf("SELECT_RESULT ok=0 error=%d sender_uid=\n",

                                     res.error());

                         std::fflush(stdout);

                         MarkScenarioFail("select_client",

                                          "select_error");

                         return;

                       }

                       g_pending_client = std::move(res).value();
                       g_select_ready_pending = true;

                     });

}



static bool HotSendAccepted(temp_sensor::prepared_send::HotSendStatus st) {

  return temp_sensor::prepared_send::HotSendConsumedNonce(st);

}



static void SendHotMessage() {

  if (g_scenario_failed) {

    ScenarioFailIdle();

  }

  if (!temp_sensor::prepared_send::HasPreparedSendBlock() ||

      temp_sensor::prepared_send::PreparedMessageLeft() == 0) {

    MarkScenarioFail("hot_prepared", "no_block");

    ScenarioFailIdle();

  }

  int const num = static_cast<int>(g_next_message);

  std::printf("HOT_BEGIN number=%d\n", num);

  std::fflush(stdout);

  auto snap =

      temp_sensor::prepared_send::SnapshotFromPreparedWifiRtcCache(g_wifi_cache);

  auto const result = temp_sensor::prepared_send::SendPreparedOnceWithFastPath(

      CachedWifi(), Payload(num), &snap);

  std::printf(

      "HOT_RESULT number=%d status=%.*s sendto_ok=%u\n", num,

      static_cast<int>(temp_sensor::prepared_send::ToString(result.status).size()),

      temp_sensor::prepared_send::ToString(result.status).data(),

      static_cast<unsigned>(result.sendto_ok));

  std::fflush(stdout);

  if (!HotSendAccepted(result.status)) {

    MarkScenarioFail("hot_send", temp_sensor::prepared_send::ToString(result.status).data());

    ScenarioFailIdle();

  }

  ++g_next_message;

  if (g_next_message > kLastMessage) {

    std::printf("SCENARIO_DONE\n");

    std::fflush(stdout);

    for (;;) {

      vTaskDelay(pdMS_TO_TICKS(1000));

      esp_task_wdt_reset();

    }

  }

  DeepSleep2s();

}



static void ColdBootUsbWindow() {

  auto const reset = esp_reset_reason();

  if (reset == ESP_RST_POWERON || reset == ESP_RST_BROWNOUT) {

    for (int i = 0; i < 60; ++i) {

      vTaskDelay(pdMS_TO_TICKS(1000));

      esp_task_wdt_reset();

    }

  }

}



void AppSetup() {

  nvs_flash_init();

  esp_event_loop_delete_default();

  std::printf("FULL_WIFI=vanilla\n");

  std::printf("HOT_WIFI=cached_channel_static_ip_static_arp\n");

  std::printf("BOOT reset_reason=%d next_message=%u\n",

              static_cast<int>(esp_reset_reason()),

              static_cast<unsigned>(g_next_message));

  std::fflush(stdout);

  if (g_next_message == kHalted) {

    MarkScenarioFail("halted", "rtc_halted");

    ScenarioFailIdle();

  }

  ColdBootUsbWindow();

  if (g_scenario_failed) {

    ScenarioFailIdle();

  }

  if (g_next_message == 1) {

    StartFullMessage();

    return;

  }

  SendHotMessage();

}



void AppLoop() {

  if (g_scenario_failed) {

    ScenarioFailIdle();

  }

  if (g_next_message != 1 || !g_app) {

    return;

  }

  g_app->Update(ae::Now());

  esp_task_wdt_reset();

  if (g_select_ready_pending && g_pending_client) {
    g_select_ready_pending = false;
    std::printf("SELECT_RESULT ok=1 error=0 sender_uid=%s\n",
                ae::Format("{}", g_pending_client->uid()).c_str());
    std::fflush(stdout);
    OnClientReady(std::move(g_pending_client));
  }

  if (g_write_ok) {

    FinishFullPath();

  }

}



}  // namespace

}  // namespace temp_sensor



void setup() { temp_sensor::AppSetup(); }



void loop() { temp_sensor::AppLoop(); }

