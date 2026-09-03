/*
 * Copyright 2026 Aethernet Inc.
 *
 * ESP32-C6 prepared-send power factor study firmware.
 * Fixed P4 hot path (no adaptive search). Measured HOT stages are silent.
 */

#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "bench_payload.h"
#include "examples/probe_receiver/power_factor_config.h"
#include "examples/probe_receiver/probe_protocol.h"
#include "experiment_early_entry.h"
#include "power_bench/power_bench_options.h"
#include "power_bench/power_bench_runtime.h"
#include "prepared_send/prepared_send.h"

#if defined(ESP_PLATFORM)
#  include <esp_attr.h>
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_random.h>
#  include <esp_sleep.h>
#  include <esp_system.h>
#  include <esp_timer.h>
#  include <esp_wifi.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#  include <soc/soc_caps.h>

extern "C" std::uint64_t esp_rtc_get_time_us(void);
#endif

namespace temp_sensor {
namespace {

namespace probe = ae::probe;

#ifndef AE_POWER_BENCH_VARIANT
#  define AE_POWER_BENCH_VARIANT 0
#endif
#ifndef AE_POWER_BENCH_ARM_MS
#  define AE_POWER_BENCH_ARM_MS 20000
#endif

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
#ifndef BENCH_CLIENT_ID
#  define BENCH_CLIENT_ID "prepared_deepsleep_5x50_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;
#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("5aade50f-00d9-4624-b097-e203cdcf1e38");
#endif

static constexpr std::uint32_t kRtcMagic = 0x50465731u;  // PFW1
static constexpr std::uint16_t kRtcVersion = 1;
#if defined(AETHER_POWER_BENCH_HOT_SLEEP_MS)
static constexpr std::uint32_t kSleepUs =
    static_cast<std::uint32_t>(AETHER_POWER_BENCH_HOT_SLEEP_MS) * 1000u;
#else
static constexpr std::uint32_t kSleepUs =
    static_cast<std::uint32_t>(ae::power_bench::kHotSleepMs) * 1000u;
#endif
#if defined(AETHER_POWER_BENCH_HOT_ATTEMPTS)
static constexpr std::uint16_t kHotAttempts =
    static_cast<std::uint16_t>(AETHER_POWER_BENCH_HOT_ATTEMPTS);
#else
static constexpr std::uint16_t kHotAttempts = ae::power_bench::kHotAttempts;
#endif

enum class Phase : std::uint8_t {
  kFullPrepare = 0,
  kFullArm = 1,
  kHot = 2,
  kFullSummary = 3,
  kDone = 4,
};

struct RtcState {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint8_t phase;
  std::uint8_t hot_armed;
  std::uint32_t session;
  std::uint16_t variant_id;
  std::uint16_t seq;
  std::uint16_t hot_attempts;
  std::uint16_t sendto_ok;
  std::uint16_t txdone_ok;
  std::uint16_t wifi_fail;
  std::uint16_t tx_unconfirmed;
  std::uint16_t bad_wakes;
  std::uint64_t sleep_arm_us;
  std::uint32_t crc;
};

#if defined(ESP_PLATFORM)
RTC_NOINIT_ATTR RtcState g_rtc{};
// Survives esp_restart() into HOT and deep-sleep wakes (RTC_DATA zero-inits on restart).
RTC_NOINIT_ATTR prepared_send::PreparedWifiRtcCache g_rtc_wifi_cache{};

static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

ExperimentEarlyEntrySnapshot g_early{};
power_bench::PowerBenchOptions g_bench{};
prepared_send::FastPathConfig g_cfg{};
prepared_send::BisectWifiCacheSnapshot g_wifi_snapshot{};

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

void InitRtc(Phase phase) {
  g_rtc = RtcState{};
  g_rtc.magic = kRtcMagic;
  g_rtc.version = kRtcVersion;
  g_rtc.phase = static_cast<std::uint8_t>(phase);
  g_rtc.session = esp_random();
  g_rtc.variant_id = static_cast<std::uint16_t>(AE_POWER_BENCH_VARIANT);
  StoreRtc();
}

ae::DataBuffer ToDataBuffer(std::uint8_t const* data, std::size_t size) {
  return ae::DataBuffer{data, data + size};
}

[[noreturn]] void RestartTo(Phase phase) {
  g_rtc.phase = static_cast<std::uint8_t>(phase);
  StoreRtc();
  esp_restart();
}

[[noreturn]] void DeepSleepHot() {
  esp_sleep_enable_timer_wakeup(kSleepUs);
  g_rtc.sleep_arm_us = esp_rtc_get_time_us();
  StoreRtc();  // CRC must cover sleep_arm_us or ValidRtc fails on wake.
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

void WritePayload(std::uint8_t const* data, std::size_t size) {
  if (!g_stream || !g_stream->stream_info().is_writable) {
    return;
  }
  g_write_armed = true;
  g_write_ok = false;
  auto& wa = g_stream->Write(ToDataBuffer(data, size));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st == ae::WriteAction::Status::kSuccess) {
      g_write_ok = true;
    } else if (st == ae::WriteAction::Status::kFail) {
      g_write_ok = false;
    }
  });
}

bool ExportPrepared(std::uint32_t reserve) {
  auto const cache_ok =
      prepared_send::CapturePreparedWifiRtcCache(&g_rtc_wifi_cache);
  auto const block_ok = cache_ok &&
                        prepared_send::ExportPreparedSendBlock(
                            g_client, kServiceUid, reserve);
  auto const left = block_ok ? prepared_send::PreparedMessageLeft() : 0u;
  (void)left;
  return block_ok && prepared_send::HasPreparedSendBlock() &&
         prepared_send::PreparedMessageLeft() == reserve;
}

void MaybeStartStreamWork() {
  if (!g_stream || !g_stream->stream_info().is_writable || g_write_armed) {
    return;
  }
  if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kFullPrepare)) {
    if (ExportPrepared(kHotAttempts)) {
      g_rtc.phase = static_cast<std::uint8_t>(Phase::kFullArm);
      StoreRtc();
      ReleaseApp();
      prepared_send::ReleaseFullAetherWifiForHotPath();
      RestartTo(Phase::kFullArm);
    }
    return;
  }
  if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kFullArm)) {
    probe::BenchArm arm{};
    arm.session = g_rtc.session;
    arm.variant_id = g_rtc.variant_id;
    arm.expected = kHotAttempts;
    std::uint8_t buf[probe::kMaxProbeMessageSize]{};
    auto const n = probe::Pack(arm, buf, sizeof(buf));
    if (n > 0) {
      WritePayload(buf, n);
    }
    // Visible even with silent sdkconfig via USB early printf path when present.
    std::printf("BENCH_ARM expected=%u sleep_us=%lu\n",
                static_cast<unsigned>(kHotAttempts),
                static_cast<unsigned long>(kSleepUs));
    std::fflush(stdout);
    return;
  }
  if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kFullSummary)) {
    probe::BenchSummary sum{};
    sum.session = g_rtc.session;
    sum.variant_id = g_rtc.variant_id;
    sum.hot_attempts = g_rtc.hot_attempts;
    sum.sendto_ok = g_rtc.sendto_ok;
    sum.txdone_ok = g_rtc.txdone_ok;
    sum.wifi_fail = g_rtc.wifi_fail;
    sum.tx_unconfirmed = g_rtc.tx_unconfirmed;
    sum.bad_wakes = g_rtc.bad_wakes;
    std::uint8_t buf[probe::kMaxProbeMessageSize]{};
    auto const n = probe::Pack(sum, buf, sizeof(buf));
    if (n > 0) {
      WritePayload(buf, n);
    }
  }
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
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeStartStreamWork(); });
  MaybeStartStreamWork();
}

void StartFullBoot() {
  g_write_armed = false;
  g_write_ok = false;
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

std::size_t BuildHotPayload(std::uint8_t flags, std::uint8_t* out,
                            std::size_t cap) {
  probe::BenchData msg{};
  msg.session = g_rtc.session;
  msg.variant_id = g_rtc.variant_id;
  msg.seq = g_rtc.seq;
  msg.flags = flags;
  return probe::Pack(msg, out, cap);
}

void RunHotOnce() {
  if (!prepared_send::PreparedWifiRtcCacheIsValid(g_rtc_wifi_cache)) {
    ++g_rtc.bad_wakes;
    StoreRtc();
    return;
  }
  g_wifi_snapshot =
      prepared_send::SnapshotFromPreparedWifiRtcCache(g_rtc_wifi_cache);
  if (!prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() == 0) {
    ++g_rtc.bad_wakes;
    StoreRtc();
    return;
  }

  ++g_rtc.hot_attempts;
  ++g_rtc.seq;
  std::uint8_t buf[probe::kMaxProbeMessageSize]{};
  auto const n = BuildHotPayload(0, buf, sizeof(buf));
  if (n == 0) {
    return;
  }

  auto const result = prepared_send::SendPreparedOnceWithFastPath(
      g_cfg, ToDataBuffer(buf, n), &g_wifi_snapshot);

  std::uint8_t flags = 0;
  if (result.sendto_ok != 0) {
    flags |= 1u;
    ++g_rtc.sendto_ok;
  }
  if (result.tx_done_confirmed != 0) {
    flags |= 2u;
    ++g_rtc.txdone_ok;
  }
  if (result.status == prepared_send::HotSendStatus::kWifiFailed) {
    flags |= 4u;
    ++g_rtc.wifi_fail;
  }
  if (result.status == prepared_send::HotSendStatus::kSentTxUnconfirmed) {
    ++g_rtc.tx_unconfirmed;
  }
  (void)flags;
  StoreRtc();
}

bool WokeFromTimerDeepSleep() {
  return static_cast<esp_reset_reason_t>(g_early.reset_reason) ==
             ESP_RST_DEEPSLEEP &&
         static_cast<esp_sleep_wakeup_cause_t>(g_early.wakeup_cause) ==
             ESP_SLEEP_WAKEUP_TIMER;
}

void PrepareOnBoot() {
  g_early = GetExperimentEarlyEntrySnapshot();
  g_bench = power_bench::BuildVariant(
      static_cast<std::uint16_t>(AE_POWER_BENCH_VARIANT), WIFI_SSID);
  (void)power_bench::ApplyRuntimeOptions(g_bench);
  power_bench::ApplyPhyCalibrationPolicy(g_bench);
  g_cfg = power_bench::MakeApFastPath(WIFI_SSID, g_bench);

  auto const reset = static_cast<esp_reset_reason_t>(g_early.reset_reason);
  bool const valid = ValidRtc(g_rtc);
  bool const cold = reset != ESP_RST_DEEPSLEEP;
  auto const phase = static_cast<Phase>(g_rtc.phase);

  if (reset == ESP_RST_BROWNOUT) {
    if (valid) {
      if (g_rtc.bad_wakes < 0xffff) {
        ++g_rtc.bad_wakes;
      }
      StoreRtc();
    } else {
      InitRtc(Phase::kFullPrepare);
    }
  } else if (reset == ESP_RST_DEEPSLEEP && valid && phase == Phase::kHot) {
    // Continue HOT deep-sleep wakes only.
  } else if (valid && phase == Phase::kHot &&
             (reset == ESP_RST_SW || reset == ESP_RST_POWERON)) {
    // RestartTo(kHot) after BENCH_ARM uses esp_restart(); keep HOT RTC state.
  } else if (!valid) {
    InitRtc(Phase::kFullPrepare);
  } else if (cold && phase == Phase::kDone) {
    InitRtc(Phase::kFullPrepare);
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

  if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kFullPrepare) ||
      g_rtc.phase == static_cast<std::uint8_t>(Phase::kFullArm) ||
      g_rtc.phase == static_cast<std::uint8_t>(Phase::kFullSummary)) {
    StartFullBoot();
    return;
  }
  if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kDone)) {
    g_idle = true;
    return;
  }
}

void loop() {
  using namespace temp_sensor;  // NOLINT
  if (g_idle) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    return;
  }

  if (g_app) {
    if (!g_app->IsExited()) {
      (void)g_app->Update(ae::Now());
      if (g_write_armed && g_write_ok) {
        ReleaseApp();
        if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kFullArm)) {
          // Match product adaptive: tear down Aether Wi-Fi, arm-wait, then
          // deep-sleep into a clean HOT boot (same-boot send panics after
          // ReleaseFullAetherWifiForHotPath).
          prepared_send::ReleaseFullAetherWifiForHotPath();
          vTaskDelay(pdMS_TO_TICKS(AE_POWER_BENCH_ARM_MS));
          g_rtc.phase = static_cast<std::uint8_t>(Phase::kHot);
          g_rtc.seq = 0;
          g_rtc.hot_armed = 1;
          StoreRtc();
          DeepSleepHot();
        } else if (g_rtc.phase ==
                   static_cast<std::uint8_t>(Phase::kFullSummary)) {
          prepared_send::ReleaseFullAetherWifiForHotPath();
          g_rtc.phase = static_cast<std::uint8_t>(Phase::kDone);
          StoreRtc();
          g_idle = true;
        }
      }
      return;
    }
    ReleaseApp();
  }

  if (g_rtc.phase == static_cast<std::uint8_t>(Phase::kHot)) {
    if (g_rtc.hot_attempts >= kHotAttempts) {
      g_rtc.phase = static_cast<std::uint8_t>(Phase::kFullSummary);
      StoreRtc();
      RestartTo(Phase::kFullSummary);
    }
    if (g_rtc.hot_armed == 0) {
      g_rtc.hot_armed = 1;
      StoreRtc();
      DeepSleepHot();
    }
    if (!WokeFromTimerDeepSleep()) {
      if (g_rtc.bad_wakes < 0xffff) {
        ++g_rtc.bad_wakes;
      }
      StoreRtc();
      DeepSleepHot();
    }
    RunHotOnce();
    DeepSleepHot();
  }
}

#endif
