/*
 * Copyright 2026 Aethernet Inc.
 *
 * Chirkov FULL vs HOT energy Test A: 10 ordinary FULL Æther sends at ~60 s
 * start-to-start cadence with deep sleep between cycles. Silent measured path.
 */

#include <cassert>
#include <cstdint>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "examples/probe_receiver/probe_protocol.h"
#include "experiment_early_entry.h"

#if defined(ESP_PLATFORM)
#  include <esp_attr.h>
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_pm.h>
#  include <esp_random.h>
#  include <esp_sleep.h>
#  include <esp_system.h>
#  include <esp_timer.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#  include <soc/soc_caps.h>

extern "C" std::uint64_t esp_rtc_get_time_us(void);
#endif

namespace temp_sensor {
namespace {

namespace probe = ae::probe;

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
#ifndef BENCH_CLIENT_ID
#  define BENCH_CLIENT_ID "reliability_full_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;
#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("5aade50f-00d9-4624-b097-e203cdcf1e38");
#endif

#ifndef AE_FULL_1MIN_ATTEMPTS
#  define AE_FULL_1MIN_ATTEMPTS 10
#endif
#ifndef AE_FULL_1MIN_PERIOD_MS
#  define AE_FULL_1MIN_PERIOD_MS 60000
#endif

static constexpr std::uint32_t kRtcMagic = 0x46564d31u;  // FVM1
static constexpr std::uint16_t kRtcVersion = 1;
static constexpr std::uint16_t kAttempts =
    static_cast<std::uint16_t>(AE_FULL_1MIN_ATTEMPTS);
static constexpr std::uint64_t kPeriodUs =
    static_cast<std::uint64_t>(AE_FULL_1MIN_PERIOD_MS) * 1000ull;
// BenchData.flags bit for FULL type (vs HOT).
static constexpr std::uint8_t kFlagFullType = 0x80;
static constexpr std::uint16_t kVariantFull = 901;

enum class Phase : std::uint8_t {
  kSend = 0,
  kDone = 1,
};

struct RtcState {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint8_t phase;
  std::uint8_t pad;
  std::uint32_t session;
  std::uint16_t seq;
  std::uint16_t sent_ok;
  std::uint64_t cycle_start_rtc_us;
  std::uint32_t crc;
};

#if defined(ESP_PLATFORM)
RTC_NOINIT_ATTR RtcState g_rtc{};

static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

ExperimentEarlyEntrySnapshot g_early{};
std::shared_ptr<ae::AetherApp> g_app;
ae::Client::ptr g_client;
std::unique_ptr<ae::P2pStream> g_stream;
ae::Subscription g_select_sub;
ae::Subscription g_stream_sub;
ae::Subscription g_write_sub;
bool g_write_armed = false;
bool g_write_ok = false;
bool g_idle = false;
bool g_had_aether_app = false;

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

void InitRtc() {
  g_rtc = RtcState{};
  g_rtc.magic = kRtcMagic;
  g_rtc.version = kRtcVersion;
  g_rtc.phase = static_cast<std::uint8_t>(Phase::kSend);
  g_rtc.session = esp_random();
  StoreRtc();
}

ae::DataBuffer ToDataBuffer(std::uint8_t const* data, std::size_t size) {
  return ae::DataBuffer{data, data + size};
}

[[noreturn]] void DeepSleepUntilNextPeriod() {
  auto const now = esp_rtc_get_time_us();
  std::int64_t sleep_us =
      static_cast<std::int64_t>(g_rtc.cycle_start_rtc_us + kPeriodUs) -
      static_cast<std::int64_t>(now);
  if (sleep_us < 1'000'000) {
    sleep_us = 1'000'000;  // overrun: start next ASAP with short sleep
  }
  esp_sleep_enable_timer_wakeup(static_cast<std::uint64_t>(sleep_us));
  StoreRtc();
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

void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

void PreConstructCleanup() {
  if (!g_had_aether_app) {
    return;
  }
#  if !AE_WIFI_USE_FULL_DEINIT
  esp_netif_deinit();
  esp_event_loop_delete_default();
#  endif
}

void ConstructAether() {
  PreConstructCleanup();
  g_had_aether_app = true;
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
        assert(loaded_reg && "AdapterRegistry load failed");
        loaded_reg->Clear();
        loaded_reg->Add(ae::WifiAdapter::ptr::Create(
            ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
            ctx.dns_resolver(), kWifiInit));
        return ap;
      }));
}

void WriteFullPayload() {
  if (!g_stream || !g_stream->stream_info().is_writable || g_write_armed) {
    return;
  }
  ++g_rtc.seq;
  StoreRtc();
  probe::BenchData msg{};
  msg.session = g_rtc.session;
  msg.variant_id = kVariantFull;
  msg.seq = g_rtc.seq;
  msg.flags = kFlagFullType;
  std::uint8_t buf[probe::kMaxProbeMessageSize]{};
  auto const n = probe::Pack(msg, buf, sizeof(buf));
  if (n == 0) {
    return;
  }
  g_write_armed = true;
  g_write_ok = false;
  auto& wa = g_stream->Write(ToDataBuffer(buf, n));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st == ae::WriteAction::Status::kSuccess) {
      g_write_ok = true;
    }
  });
}

void OnClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  auto client = g_client.Load();
  auto cloud = client->cloud().Load();
  if (cloud) {
    for (auto& [sid, cs] : cloud->servers()) {
      auto server = cs.server.Load();
      if (server) {
        server->RebuildChannelsFromAdapters();
      }
    }
  }
  auto handle = client->message_stream_manager().CreatePort(kServiceUid);
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             std::move(handle));
  g_stream_sub = g_stream->stream_update_event().Subscribe([]() {
    WriteFullPayload();
  });
  WriteFullPayload();
}

void StartFullBoot() {
  g_write_armed = false;
  g_write_ok = false;
  g_rtc.cycle_start_rtc_us = esp_rtc_get_time_us();
  StoreRtc();
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       OnClientReady(std::move(res).value());
                     });
}

void PrepareOnBoot() {
  g_early = GetExperimentEarlyEntrySnapshot();
  auto const reset = static_cast<esp_reset_reason_t>(g_early.reset_reason);
  bool const valid = ValidRtc(g_rtc);
  if (!valid || reset == ESP_RST_POWERON ||
      (reset != ESP_RST_DEEPSLEEP && reset != ESP_RST_SW)) {
    InitRtc();
  }
}

#endif  // ESP_PLATFORM

}  // namespace
}  // namespace temp_sensor

#if defined(ESP_PLATFORM)

void setup() {
  using namespace temp_sensor;  // NOLINT
  nvs_flash_init();
  g_idle = false;
  PrepareOnBoot();
  if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kDone) ||
      g_rtc.seq >= kAttempts) {
    g_rtc.phase = static_cast<std::uint8_t>(Phase::kDone);
    StoreRtc();
    g_idle = true;
    return;
  }
  StartFullBoot();
}

void loop() {
  using namespace temp_sensor;  // NOLINT
  if (g_idle) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    return;
  }
  if (!g_app) {
    return;
  }
  if (!g_app->IsExited()) {
    (void)g_app->Update(ae::Now());
    if (g_write_armed && g_write_ok) {
      ++g_rtc.sent_ok;
      StoreRtc();
      ReleaseApp();
      if (g_rtc.seq >= kAttempts) {
        g_rtc.phase = static_cast<std::uint8_t>(Phase::kDone);
        StoreRtc();
        DeepSleepUntilNextPeriod();
      }
      DeepSleepUntilNextPeriod();
    }
    return;
  }
  ReleaseApp();
  DeepSleepUntilNextPeriod();
}

#endif
