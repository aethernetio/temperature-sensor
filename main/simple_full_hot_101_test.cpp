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

#include <esp_timer.h>

#include <functional>

#include "bench_payload.h"



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

struct BootRtcState {
  static constexpr std::uint32_t kMagic = 0x53464831u;
  std::uint32_t magic{kMagic};
  std::uint8_t bootstrap_complete{0};
  std::uint8_t pad[3]{};
  std::uint8_t sender_uid[16]{};
  std::uint32_t crc{0};
};

RTC_DATA_ATTR static BootRtcState g_boot_rtc{};

static bool g_bootstrap_active = false;
static bool g_bootstrap_select_done = false;
static bool g_bootstrap_select_ok = false;
static int g_bootstrap_select_err = 0;
static bool g_bootstrap_write_armed = false;
static bool g_bootstrap_write_terminal = false;
static ae::WriteAction::Status g_bootstrap_write_status =
    ae::WriteAction::Status::kStop;
static ae::Subscription g_bootstrap_select_sub;
static ae::Subscription g_bootstrap_write_sub;



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



static std::uint32_t BootRtcCrc32(void const* data, std::size_t len) {
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



static bool ValidBootRtc(BootRtcState const& s) {
  if (s.magic != BootRtcState::kMagic) {
    return false;
  }
  BootRtcState tmp = s;
  tmp.crc = 0;
  return BootRtcCrc32(&tmp, sizeof(tmp)) == s.crc;
}



static void StoreBootRtc() {
  g_boot_rtc.crc = 0;
  g_boot_rtc.crc = BootRtcCrc32(&g_boot_rtc, sizeof(g_boot_rtc));
}



static void StoreSenderUid(ae::Uid const& uid) {
  std::memcpy(g_boot_rtc.sender_uid, uid.value.data(), ae::Uid::kSize);
  StoreBootRtc();
}



static bool SenderUidMatchesRtc(ae::Uid const& uid) {
  if (g_boot_rtc.bootstrap_complete == 0) {
    return true;
  }
  return std::memcmp(g_boot_rtc.sender_uid, uid.value.data(), ae::Uid::kSize) ==
         0;
}



static void PumpAppUntil(std::function<bool()> done) {
  if (!g_app) {
    return;
  }
  auto const deadline_us = esp_timer_get_time() + 180LL * 1000000;
  while (!done() && esp_timer_get_time() < deadline_us && !g_scenario_failed) {
    auto const t = g_app->Update(ae::Now());
    g_app->WaitUntil(t);
    esp_task_wdt_reset();
  }
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



static void BootstrapTryWrite() {

  if (!g_bootstrap_active || !g_stream || g_bootstrap_write_armed) {

    return;

  }

  if (!g_stream->stream_info().is_writable) {

    return;

  }

  temp_sensor::bench::ReliabilityPayload p{};

  p.type = static_cast<std::uint8_t>(
      temp_sensor::bench::ReliabilityMsgType::kFullReady);

  p.run_id = 0;

  p.seq = 0;

  auto payload = temp_sensor::bench::EncodeReliability<ae::DataBuffer>(p);

  g_bootstrap_write_armed = true;

  std::printf("BOOTSTRAP_WRITE_BEGIN\n");

  std::fflush(stdout);

  auto& wa = g_stream->Write(std::move(payload));

  g_bootstrap_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {

    g_bootstrap_write_status = st;

    if (st == ae::WriteAction::Status::kSuccess ||

        st == ae::WriteAction::Status::kFail ||

        st == ae::WriteAction::Status::kStop) {

      g_bootstrap_write_terminal = true;

      std::printf("BOOTSTRAP_WRITE_STATUS=%s\n", WriteStatusName(st));

      std::fflush(stdout);

    }

  });

}



static void OnClientReady(ae::Client::ptr client) {

  g_client = std::move(client);

  auto loaded = g_client.Load();

  if (!loaded) {

    MarkScenarioFail("select_client", "client_load_failed");

    return;

  }

  if (!SenderUidMatchesRtc(loaded->uid())) {

    MarkScenarioFail("select_client", "sender_uid_mismatch");

    return;

  }

  auto handle = loaded->message_stream_manager().CreatePort(kServiceUid);

  g_stream = std::make_unique<ae::P2pStream>(*g_app, loaded, kServiceUid,

                                               std::move(handle));

  g_stream_sub = g_stream->stream_update_event().Subscribe([]() {

    if (g_bootstrap_active) {

      BootstrapTryWrite();

      return;

    }

    TryFullWrite();

  });

  if (g_bootstrap_active) {

    BootstrapTryWrite();

  } else {

    TryFullWrite();

  }

}



static void StartBootstrapPhase() {

  g_bootstrap_active = true;

  g_bootstrap_select_done = false;

  g_bootstrap_select_ok = false;

  g_bootstrap_select_err = 0;

  g_bootstrap_write_armed = false;

  g_bootstrap_write_terminal = false;

  std::printf("BOOTSTRAP_BEGIN client_id=%s FS_INIT=0\n", kBenchClientId);

  std::fflush(stdout);

  ConstructVanillaAether();

  for (int i = 0; i < 450 && g_app; ++i) {

    auto const t = g_app->Update(ae::Now());

    g_app->WaitUntil(t);

    esp_task_wdt_reset();

  }

  g_bootstrap_select_sub = g_app->aether()

                                 ->SelectClient(kParentUid, kBenchClientId)

                                 .result_event()

                                 .Subscribe([](ae::Result<ae::Client::ptr, int> res) {

                                   g_bootstrap_select_done = true;

                                   if (!res) {

                                     g_bootstrap_select_err = res.error();

                                     std::printf(

                                         "BOOTSTRAP_SELECT ok=0 error=%d\n",

                                         g_bootstrap_select_err);

                                     std::fflush(stdout);

                                     MarkScenarioFail("bootstrap_select",

                                                      "select_error");

                                     return;

                                   }

                                   auto client = std::move(res).value();

                                   if (!client.Load()) {

                                     g_bootstrap_select_ok = false;

                                     g_bootstrap_select_err = 3;

                                     std::printf("BOOTSTRAP_SELECT ok=0 error=3\n");

                                     std::fflush(stdout);

                                     MarkScenarioFail("bootstrap_select",

                                                      "client_load_failed");

                                     return;

                                   }

                                   g_bootstrap_select_ok = true;

                                   std::printf(

                                       "BOOTSTRAP_SELECT ok=1 sender_uid=%s\n",

                                       ae::Format("{}", client.Load()->uid())

                                           .c_str());

                                   std::fflush(stdout);

                                   OnClientReady(std::move(client));

                                 });

}



static void FinishBootstrapPhase() {

  g_bootstrap_active = false;

  g_bootstrap_select_sub.Reset();

  g_bootstrap_write_sub.Reset();

  if (g_client.is_valid()) {

    StoreSenderUid(g_client.Load()->uid());

  }

  g_app->aether().Save();

  for (int i = 0; i < 400 && g_app; ++i) {

    auto const t = g_app->Update(ae::Now());

    g_app->WaitUntil(t);

    esp_task_wdt_reset();

  }

  ReleaseApp();

  g_boot_rtc.bootstrap_complete = 1;

  StoreBootRtc();

  std::printf("BOOTSTRAP_DONE sender_uid_stored=1\n");

  std::fflush(stdout);

  DeepSleep2s();

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

                         int const err = res.error();

                         std::printf("SELECT_RESULT ok=0 error=%d sender_uid=\n",

                                     err);

                         std::fflush(stdout);

                         MarkScenarioFail(

                             "select_client",

                             err == 3 ? "client_map_load_failed" : "select_error");

                         return;

                       }

                       auto client = std::move(res).value();

                       if (!client.Load()) {

                         std::printf("SELECT_RESULT ok=0 error=3 sender_uid=\n");

                         std::fflush(stdout);

                         MarkScenarioFail("select_client", "client_map_load_failed");

                         return;

                       }

                       g_pending_client = std::move(client);
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

  std::printf("BOOT reset_reason=%d next_message=%u bootstrap=%u\n",

              static_cast<int>(esp_reset_reason()),

              static_cast<unsigned>(g_next_message),

              static_cast<unsigned>(ValidBootRtc(g_boot_rtc)

                                        ? g_boot_rtc.bootstrap_complete

                                        : 0));

  std::fflush(stdout);

  if (!ValidBootRtc(g_boot_rtc)) {

    g_boot_rtc = BootRtcState{};

    StoreBootRtc();

  }

  if (g_next_message == kHalted) {

    MarkScenarioFail("halted", "rtc_halted");

    ScenarioFailIdle();

  }

  ColdBootUsbWindow();

  if (g_boot_rtc.bootstrap_complete == 0) {

    StartBootstrapPhase();

    return;

  }

  if (g_scenario_failed) {

    ScenarioFailIdle();

  }

  std::printf("CLIENT_RELOAD_CHECK client_id=%s bootstrap=1\n", kBenchClientId);

  std::fflush(stdout);

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

  if (g_bootstrap_active && g_app) {

    PumpAppUntil([]() {

      if (g_bootstrap_select_done && !g_bootstrap_select_ok) {

        return true;

      }

      return g_bootstrap_select_done && g_bootstrap_write_terminal;

    });

    if (g_bootstrap_select_done && !g_bootstrap_select_ok) {

      ScenarioFailIdle();

    }

    if (g_bootstrap_write_terminal) {

      if (g_bootstrap_write_status == ae::WriteAction::Status::kSuccess) {

        FinishBootstrapPhase();

      } else {

        MarkScenarioFail("bootstrap_write", WriteStatusName(g_bootstrap_write_status));

        ScenarioFailIdle();

      }

    }

    return;

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

