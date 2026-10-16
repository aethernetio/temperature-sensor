/*
 * Thermometer 2: one full Æther P2pStream Write per timer wake, vanilla Wi-Fi,
 * 20 s deep sleep between cycles. No prepared/HOT/cache path.
 */

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/adapters/wifi_adapter.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "aether/ae_exp_save_stats.h"
#include "bench_payload.h"
#include "sleeping/board_sleep_powerdown.h"

#if defined(ESP_PLATFORM)
#  include <esp_attr.h>
#  include <esp_random.h>
#  include <esp_sleep.h>
#  include <esp_wifi.h>
#  include <esp_system.h>
#  include <esp_timer.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#  include <soc/soc_caps.h>

extern "C" std::uint64_t esp_rtc_get_time_us(void);
#endif

#ifndef BENCH_CLIENT_ID
#  define BENCH_CLIENT_ID "reliability_full_v1"
#endif
#ifndef AE_THERM_FULL_VANILLA_SLEEP_S
#  define AE_THERM_FULL_VANILLA_SLEEP_S 20
#endif
#ifndef AE_THERM_FULL_ACTIVE_TIMEOUT_MS
#  define AE_THERM_FULL_ACTIVE_TIMEOUT_MS 60000
#endif
#ifndef AE_THERM_FULL_MEASURE_CYCLES
#  define AE_THERM_FULL_MEASURE_CYCLES 100
#endif

namespace temp_sensor {
namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("00000000-0000-0000-0000-000000000000");
#endif

static constexpr std::uint32_t kRtcMagic = 0x54465631u;  // TFV1
static constexpr std::uint16_t kRtcVersion = 1;
static constexpr std::uint64_t kSleepUs =
    static_cast<std::uint64_t>(AE_THERM_FULL_VANILLA_SLEEP_S) * 1000000ull;

enum class FailStage : std::uint8_t {
  kNone = 0,
  kTimeout,
  kSelectClient,
  kStream,
  kWriteNotCalled,
  kWriteTerminal,
  kSave,
};

struct RtcState {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint16_t cycle;  // next cycle index (1-based after increment)
  std::uint32_t session;
  std::uint8_t measure_armed;
  std::uint8_t last_fail_stage;
  std::uint8_t pad[2];
  std::uint32_t crc;
};

#if defined(ESP_PLATFORM)
RTC_NOINIT_ATTR RtcState g_rtc{};

static ae::WiFiInit MakeVanillaWifiInit() {
  ae::WiFiPowerSaveParam psp{};
  psp.wifi_ps_type = WIFI_PS_NONE;
  return ae::WiFiInit{
      std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
      psp,
  };
}

std::shared_ptr<ae::AetherApp> g_app;
ae::Client::ptr g_client;
std::unique_ptr<ae::P2pStream> g_stream;
ae::Subscription g_select_sub;
ae::Subscription g_stream_sub;
ae::Subscription g_write_sub;

bool g_select_done = false;
bool g_select_ok = false;
bool g_write_armed = false;
bool g_write_terminal = false;
ae::WriteAction::Status g_write_status = ae::WriteAction::Status::kStop;
bool g_got_ip_logged = false;
bool g_cycle_active = false;
FailStage g_fail = FailStage::kNone;
std::int64_t g_active_t0_us = 0;
std::int64_t g_deadline_us = 0;

void StageLog(char const* line) {
#if defined(AE_EXP_BENCH_STAGE) && AE_EXP_BENCH_STAGE
  std::printf("%s\n", line);
  std::fflush(stdout);
#else
  (void)line;
#endif
}

std::uint32_t Crc32(void const* data, std::size_t len) {
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

bool ValidRtc(RtcState const& s) {
  if (s.magic != kRtcMagic || s.version != kRtcVersion) {
    return false;
  }
  RtcState tmp = s;
  tmp.crc = 0;
  return Crc32(&tmp, sizeof(tmp)) == s.crc;
}

void StoreRtc() {
  g_rtc.crc = 0;
  g_rtc.crc = Crc32(&g_rtc, sizeof(g_rtc));
}

void InitRtc(std::uint32_t session) {
  g_rtc = RtcState{};
  g_rtc.magic = kRtcMagic;
  g_rtc.version = kRtcVersion;
  g_rtc.cycle = 0;
  g_rtc.session = session;
  g_rtc.measure_armed = 0;
  StoreRtc();
}

std::uint32_t MessageRunId() {
  return (g_rtc.session << 16) | (static_cast<std::uint32_t>(g_rtc.cycle) & 0xffffu);
}

#if AE_DISTILLATION
static void ReleaseWifiDriversBeforeAppReset() {
  if (!g_app || !g_app->aether() || !g_app->aether()->adapter_registry) {
    return;
  }
  for (auto const& adapter : g_app->aether()->adapter_registry->adapters()) {
    ae::WifiAdapter::ptr wa{adapter};
    wa.WithLoaded([](auto const& a) {
      a->ReleaseOwnedDriverForPhaseHandoff();
    });
  }
}
#endif

void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
#if AE_DISTILLATION
  ReleaseWifiDriversBeforeAppReset();
#endif
  g_app.reset();
}

void ConstructAether() {
  g_app = ae::AetherApp::Construct(ae::AetherAppContext{}.AdaptersFactory(
      [&](ae::AetherAppContext const& ctx) {
        auto ap = ae::AdapterRegistry::ptr{ctx.aether()->adapter_registry};
        if (!ap.is_valid()) {
          ap = ae::AdapterRegistry::ptr::Create(
              ae::CreateWith{ctx.domain()}
                  .with_id(ae::GlobalId::kAdapterRegistry)
                  .with_flags(ae::ObjFlags::kUnloadedByDefault));
        }
        auto loaded_reg = ap.Load();
        loaded_reg->Clear();
        loaded_reg->Add(ae::WifiAdapter::ptr::Create(
            ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
            ctx.dns_resolver(), MakeVanillaWifiInit()));
        return ap;
      }));
}

template <typename Pred>
void PumpUntil(Pred&& done) {
  while (!done()) {
    auto const now = ae::Now();
    if (esp_timer_get_time() >= g_deadline_us) {
      return;
    }
    auto const t = g_app->Update(now);
    if (done()) {
      return;
    }
    g_app->WaitUntil(t);
  }
}

void LogWifiIfConnected() {
  if (g_got_ip_logged || !g_app || !g_app->aether()->adapter_registry) {
    return;
  }
  for (auto const& adapter : g_app->aether()->adapter_registry->adapters()) {
    ae::WifiAdapter::ptr wa{adapter};
    wa.WithLoaded([](auto const& a) {
      for (auto const& ap : a->access_points()) {
        ae::WifiAccessPoint::ptr wap{ap};
        wap.WithLoaded([&](auto const& p) {
          if (p && p->IsConnected()) {
            StageLog("GOT_IP");
            g_got_ip_logged = true;
          }
        });
      }
    });
  }
}

void MaybeWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  temp_sensor::bench::ReliabilityPayload p{};
  p.type = static_cast<std::uint8_t>(temp_sensor::bench::ReliabilityMsgType::kFull);
  p.run_id = MessageRunId();
  p.seq = 0;
  auto payload = temp_sensor::bench::EncodeReliability<ae::DataBuffer>(p);
  g_write_armed = true;
  StageLog("WRITE_BEGIN");
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st == ae::WriteAction::Status::kSuccess ||
        st == ae::WriteAction::Status::kFail ||
        st == ae::WriteAction::Status::kStop) {
      g_write_terminal = true;
      g_write_status = st;
      std::printf("WRITE_TERMINAL status=%d run_id=%lu\n",
                  static_cast<int>(st),
                  static_cast<unsigned long>(MessageRunId()));
      std::fflush(stdout);
    }
  });
}

void OnClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  auto client = g_client.Load();
  auto handle = client->message_stream_manager().CreatePort(kServiceUid);
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             std::move(handle));
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeWrite(); });
  StageLog("STREAM_STATE created");
  MaybeWrite();
}

void StartCycle() {
  g_select_done = false;
  g_select_ok = false;
  g_write_armed = false;
  g_write_terminal = false;
  g_got_ip_logged = false;
  g_fail = FailStage::kNone;
  g_active_t0_us = esp_timer_get_time();
  g_deadline_us =
      g_active_t0_us + static_cast<std::int64_t>(AE_THERM_FULL_ACTIVE_TIMEOUT_MS) * 1000;

  ++g_rtc.cycle;
  StoreRtc();

  std::printf("BOOT cycle=%u session=%lu run_id=%lu\n",
              static_cast<unsigned>(g_rtc.cycle),
              static_cast<unsigned long>(g_rtc.session),
              static_cast<unsigned long>(MessageRunId()));
  std::fflush(stdout);

  StageLog("WIFI_START");
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       g_select_done = true;
                       if (res) {
                         g_select_ok = true;
                         std::printf("SELECT_CLIENT_RESULT ok=1 uid=%s\n",
                                     ae::Format("{}", res.value()->uid())
                                         .c_str());
                         std::fflush(stdout);
                         OnClientReady(std::move(res).value());
                       } else {
                         std::printf("SELECT_CLIENT_RESULT ok=0 err=%d\n",
                                     res.error());
                         std::fflush(stdout);
                       }
                     });
  g_cycle_active = true;
}

void FinishCycleAndSleep() {
  g_cycle_active = false;
  if (g_app && g_app->aether()) {
    AeExpSaveSetCaller(AeExpSaveCaller::kNone);
    g_app->aether().Save();
    StageLog("STATE_SAVED");
  }
  ReleaseApp();
  StageLog("NETWORK_STOPPED");
  BoardPrepareDeepSleep(1);
  esp_sleep_enable_timer_wakeup(kSleepUs);
  StoreRtc();
  std::printf("DEEP_SLEEP_ENTER sleep_s=%d\n", AE_THERM_FULL_VANILLA_SLEEP_S);
  std::fflush(stdout);
  (void)esp_deep_sleep_try_to_start();
  esp_deep_sleep_start();
}

[[noreturn]] void IdleAfterCampaign() {
  StageLog("CAMPAIGN_DONE");
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

#endif  // ESP_PLATFORM

}  // namespace
}  // namespace temp_sensor

#if defined(ESP_PLATFORM)

void setup() {
  using namespace temp_sensor;
  nvs_flash_init();
  auto const reset = esp_reset_reason();
  if (reset == ESP_RST_POWERON || reset == ESP_RST_BROWNOUT) {
    StageLog("BOOT cold_power_on");
    StageLog("AWAKE_USB_WINDOW begin 60s");
    vTaskDelay(pdMS_TO_TICKS(60000));
    StageLog("AWAKE_USB_WINDOW end");
  } else {
    StageLog("BOOT timer_wake");
  }

  if (!ValidRtc(g_rtc)) {
    InitRtc(static_cast<std::uint32_t>(esp_random()));
  }

  if (g_rtc.cycle >= AE_THERM_FULL_MEASURE_CYCLES &&
      g_rtc.measure_armed != 0) {
    IdleAfterCampaign();
  }

  if (g_rtc.cycle == 0) {
    g_rtc.measure_armed = 1;
    StoreRtc();
  }

  StartCycle();
}

void loop() {
  using namespace temp_sensor;
  if (!g_cycle_active || !g_app) {
    return;
  }

  PumpUntil([]() { return g_select_done && g_write_terminal; });
  LogWifiIfConnected();

  if (esp_timer_get_time() >= g_deadline_us && !g_write_terminal) {
    g_fail = FailStage::kTimeout;
    std::printf("CYCLE_FAIL stage=timeout cycle=%u\n",
                static_cast<unsigned>(g_rtc.cycle));
    std::fflush(stdout);
    FinishCycleAndSleep();
    return;
  }

  if (g_select_done && !g_select_ok) {
    g_fail = FailStage::kSelectClient;
    g_rtc.last_fail_stage = static_cast<std::uint8_t>(g_fail);
    StoreRtc();
    std::printf("CYCLE_FAIL stage=select_client cycle=%u\n",
                static_cast<unsigned>(g_rtc.cycle));
    std::fflush(stdout);
    FinishCycleAndSleep();
    return;
  }

  if (!g_write_armed) {
    g_fail = FailStage::kWriteNotCalled;
  } else if (!g_write_terminal) {
    return;
  } else if (g_write_status != ae::WriteAction::Status::kSuccess) {
    g_fail = FailStage::kWriteTerminal;
  }

  if (g_fail != FailStage::kNone) {
    g_rtc.last_fail_stage = static_cast<std::uint8_t>(g_fail);
    StoreRtc();
    std::printf("CYCLE_FAIL stage=%u cycle=%u\n",
                static_cast<unsigned>(g_rtc.last_fail_stage),
                static_cast<unsigned>(g_rtc.cycle));
    std::fflush(stdout);
    FinishCycleAndSleep();
    return;
  }

  auto const hold_until =
      esp_timer_get_time() + static_cast<std::int64_t>(1000) * 1000;
  while (esp_timer_get_time() < hold_until) {
    auto const t = g_app->Update(ae::Now());
    g_app->WaitUntil(t);
    LogWifiIfConnected();
  }

  FinishCycleAndSleep();
}

#endif
