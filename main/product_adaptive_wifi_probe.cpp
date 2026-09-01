/*
 * Copyright 2026 Aethernet Inc.
 *
 * Product adaptive Wi-Fi probe.
 *
 * The device configures its own hot-path Wi-Fi parameters on first boot and
 * then runs a production hot send campaign with them. Nothing is hardcoded:
 * the profile, the PRE delay and the POST delay all come out of measurements
 * made against whatever access point the device is attached to.
 *
 * Stage flow, one stage per boot, every transition a 250 ms timer deep sleep:
 *
 *   0 ICMP_SELECT          raw Wi-Fi, pick profile then PRE delay
 *   1 FULL_PREPARE         Aether FULL: marker + prepared block for stage 2
 *   2 POST_PROBE           20 prepared sends, no sleep between them
 *   3 POST_QUERY           Aether FULL: query the batch, judge the POST delay
 *   4 SLEEP_CONFIRM        20 prepared sends with 250 ms deep sleep between
 *   5 SLEEP_CONFIRM_QUERY  Aether FULL: query the sleep-confirm batch
 *   6 PROBE_COMPLETE       Aether FULL: marker + prepared block for stage 7
 *   7 HOT_RUN              100 prepared HOT sends, 250 ms deep sleep between
 *   8 HOT_SUMMARY          Aether FULL: totals
 *   9 DONE                 idle
 *
 * Only a deep-sleep timer wake preserves the stage. Any other reset restarts
 * at stage 0, which is also what a pre-Encode Wi-Fi failure on the hot path
 * forces: the network may have changed, so the selected parameters are no
 * longer trusted.
 *
 * Stages 2, 4 and 7 are measured and must not print.
 */

#include <cassert>
#include <cstdint>
#include <cstring>
#include <memory>

#include "aether/ae_exp_wifi.h"
#include "aether/all.h"
#include "aether/config.h"
#include "aether/env.h"
#include "aether/wifi/wifi_gateway_probe.h"
#include "aether/wifi/wifi_probe_state.h"
#include "bench_payload.h"
#include "examples/probe_receiver/probe_protocol.h"
#include "examples/probe_receiver/product_probe_select.h"
#include "experiment_early_entry.h"
#include "prepared_send/prepared_send.h"

#if defined(ESP_PLATFORM)
#  include <cstdio>

#  include <esp_attr.h>
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_random.h>
#  include <esp_sleep.h>
#  include <esp_system.h>
#  include <esp_timer.h>
#  include <esp_wifi.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/event_groups.h>
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
#  define BENCH_CLIENT_ID "prepared_deepsleep_5x50_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("5aade50f-00d9-4624-b097-e203cdcf1e38");
#endif

// Stage transition sleep, and the sleep between sleep-confirm and hot sends.
static constexpr std::uint32_t kStageSleepUs =
    static_cast<std::uint32_t>(probe::kProbeSleepMs) * 1000u;

// Prepared attempts allowed inside one batch before the probe restarts.
static constexpr std::uint16_t kMaxBatchAttempts =
    static_cast<std::uint16_t>(probe::kProbeHotCount + 40);

// Probe batches allowed per stage before the current best value is accepted.
static constexpr std::uint16_t kMaxProbeBatchesPerStage = 12;

// Wall clock budget for one Aether FULL boot.
static constexpr std::uint32_t kFullStageTimeoutMs = 60000;

// POST ladder used when a confirmation batch has to be retried more
// conservatively than the descending search suggested.
static constexpr std::uint16_t kPostLadder[] = {0, 10, 25, 50, 100, 200, 300};

#if defined(ESP_PLATFORM)

// Which piece of work the current Aether FULL boot performs.
enum class FullPurpose : std::uint8_t {
  kPrepareBatch,
  kQueryPostBatch,
  kQuerySleepBatch,
  kProbeComplete,
  kHotSummary,
};

// Last raw Wi-Fi association, used by the cached-IP/channel ICMP profiles.
struct IcmpCache {
  std::uint8_t channel{0};
  std::uint32_t ip{0};
  std::uint32_t netmask{0};
  std::uint32_t gateway{0};
  std::uint8_t authmode{0};
  std::uint8_t valid{0};
};

RTC_DATA_ATTR probe::ProbeRtcState g_rtc{};
RTC_DATA_ATTR prepared_send::PreparedWifiRtcCache g_rtc_wifi_cache{};
RTC_DATA_ATTR IcmpCache g_icmp_cache{};
RTC_DATA_ATTR std::uint16_t g_rtc_batch_attempts{0};
RTC_DATA_ATTR std::uint16_t g_rtc_stage_batches{0};
RTC_DATA_ATTR std::uint64_t g_rtc_sleep_arm_us{0};

static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

ExperimentEarlyEntrySnapshot g_early{};
std::uint32_t g_sleep_elapsed_us = 0;

std::shared_ptr<ae::AetherApp> g_app;
ae::Client::ptr g_client;
std::unique_ptr<ae::P2pStream> g_stream;
ae::Subscription g_select_sub;
ae::Subscription g_stream_sub;
ae::Subscription g_data_sub;
ae::Subscription g_write_sub;

FullPurpose g_purpose{FullPurpose::kPrepareBatch};
bool g_had_aether_app = false;
bool g_write_armed = false;
bool g_write_ok = false;
bool g_write_status_known = false;
bool g_stage_ok = false;
bool g_finished_in_loop = false;
bool g_idle = false;
std::uint32_t g_full_start_ms = 0;

probe::LateQueryState g_query{};
bool g_query_have_result = false;
bool g_query_finished = false;
bool g_query_timed_out = false;
std::uint16_t g_query_unique = 0;

prepared_send::FastPathConfig g_cfg{};
prepared_send::BisectWifiCacheSnapshot g_wifi_snapshot{};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

std::uint32_t NowMs() {
  return static_cast<std::uint32_t>(esp_timer_get_time() / 1000);
}

probe::ProbeStage Stage() { return probe::ProductProbeStage(g_rtc); }

// Measured stages must stay silent: any console write distorts the cycle time.
bool StageIsMeasured(probe::ProbeStage stage) {
  return stage == probe::ProbeStage::kPostProbe ||
         stage == probe::ProbeStage::kSleepConfirm ||
         stage == probe::ProbeStage::kHotRun;
}

void SayStage(char const* tag) {
  if (StageIsMeasured(Stage())) {
    return;
  }
  std::printf(
      "P_STAGE stage=%u name=%s tag=%s session=%08lx profile=%u pre=%u post=%u "
      "sleep=%u batch=%u param=%u seq=%u hot=%u fail=%u reprobe=%u\n",
      static_cast<unsigned>(g_rtc.stage), probe::ProbeStageName(Stage()), tag,
      static_cast<unsigned long>(g_rtc.session),
      static_cast<unsigned>(g_rtc.profile),
      static_cast<unsigned>(g_rtc.pre_ms),
      static_cast<unsigned>(g_rtc.post_ms),
      static_cast<unsigned>(g_rtc.sleep_ms),
      static_cast<unsigned>(g_rtc.batch_id),
      static_cast<unsigned>(g_rtc.parameter_id),
      static_cast<unsigned>(g_rtc.seq), static_cast<unsigned>(g_rtc.hot_sent),
      static_cast<unsigned>(g_rtc.hot_fail),
      static_cast<unsigned>(g_rtc.reprobe_count));
  std::fflush(stdout);
}

[[noreturn]] void DeepSleepToNextStage() {
  esp_sleep_enable_timer_wakeup(kStageSleepUs);
  g_rtc_sleep_arm_us = esp_rtc_get_time_us();
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_ON);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_ON);
#  endif
  esp_deep_sleep_try_to_start();
  esp_deep_sleep_start();
  for (;;) {
  }
}

void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_data_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

[[noreturn]] void Reprobe(char const* reason) {
  ReleaseApp();
  g_rtc_wifi_cache = prepared_send::PreparedWifiRtcCache{};
  g_icmp_cache = IcmpCache{};
  g_rtc_batch_attempts = 0;
  g_rtc_stage_batches = 0;
  probe::ProductProbeFailureResetStage(g_rtc);
  std::printf("P_REPROBE reason=%s count=%u\n", reason,
              static_cast<unsigned>(g_rtc.reprobe_count));
  std::fflush(stdout);
  DeepSleepToNextStage();
}

ae::DataBuffer ToDataBuffer(std::uint8_t const* data, std::size_t size) {
  return ae::DataBuffer{data, data + size};
}

// Next more conservative POST value, or the current one when already highest.
std::uint16_t RaisePost(std::uint16_t current) {
  constexpr auto kCount = sizeof(kPostLadder) / sizeof(kPostLadder[0]);
  for (std::size_t i = 0; i < kCount; ++i) {
    if (kPostLadder[i] > current) {
      return kPostLadder[i];
    }
  }
  return kPostLadder[kCount - 1];
}

// ---------------------------------------------------------------------------
// Stage 0: raw Wi-Fi ICMP profile and PRE selection
// ---------------------------------------------------------------------------

constexpr int kWifiConnectedBit = BIT0;
constexpr int kWifiFailBit = BIT1;

EventGroupHandle_t g_icmp_eg = nullptr;
esp_netif_t* g_icmp_netif = nullptr;
int g_icmp_retry = 0;
bool g_icmp_wait_ip = true;

void OnIcmpWifiEvent(void*, esp_event_base_t base, std::int32_t id, void*) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    esp_wifi_connect();
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    if (++g_icmp_retry < 10) {
      esp_wifi_connect();
    } else if (g_icmp_eg != nullptr) {
      xEventGroupSetBits(g_icmp_eg, kWifiFailBit);
    }
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
    if (!g_icmp_wait_ip && g_icmp_eg != nullptr) {
      xEventGroupSetBits(g_icmp_eg, kWifiConnectedBit);
    }
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    if (g_icmp_eg != nullptr) {
      xEventGroupSetBits(g_icmp_eg, kWifiConnectedBit);
    }
  }
}

void IcmpTeardown() {
  esp_event_handler_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnIcmpWifiEvent);
  esp_event_handler_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP,
                               &OnIcmpWifiEvent);
  esp_wifi_disconnect();
  esp_wifi_stop();
  esp_wifi_deinit();
  if (g_icmp_netif != nullptr) {
    esp_netif_destroy_default_wifi(g_icmp_netif);
    g_icmp_netif = nullptr;
  }
  if (g_icmp_eg != nullptr) {
    vEventGroupDelete(g_icmp_eg);
    g_icmp_eg = nullptr;
  }
  esp_event_loop_delete_default();
}

bool IcmpConnect(ae::WifiProbeProfile profile, std::uint16_t pre_ms,
                 std::uint32_t* connect_ms, std::uint32_t* gateway_be) {
  *connect_ms = 0;
  *gateway_be = 0;
  g_icmp_retry = 0;

  bool const use_ip = ae::WifiProbeProfileUsesCachedIp(profile) &&
                      g_icmp_cache.valid != 0 && g_icmp_cache.ip != 0;
  bool const use_channel = ae::WifiProbeProfileUsesChannel(profile) &&
                           g_icmp_cache.valid != 0 &&
                           g_icmp_cache.channel != 0;
  g_icmp_wait_ip = !use_ip;

  nvs_flash_init();
  esp_netif_init();
  esp_event_loop_create_default();
  g_icmp_eg = xEventGroupCreate();
  g_icmp_netif = esp_netif_create_default_wifi_sta();

  if (use_ip) {
    esp_netif_dhcpc_stop(g_icmp_netif);
    esp_netif_ip_info_t ip{};
    ip.ip.addr = g_icmp_cache.ip;
    ip.netmask.addr = g_icmp_cache.netmask;
    ip.gw.addr = g_icmp_cache.gateway;
    esp_netif_set_ip_info(g_icmp_netif, &ip);
  }

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  cfg.ampdu_rx_enable = 0;
  cfg.ampdu_tx_enable = 0;
  esp_wifi_init(&cfg);
  esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnIcmpWifiEvent,
                             nullptr);
  esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &OnIcmpWifiEvent,
                             nullptr);

  wifi_config_t wc{};
  std::strncpy(reinterpret_cast<char*>(wc.sta.ssid), WIFI_SSID,
               sizeof(wc.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wc.sta.password), WIFI_PASSWORD,
               sizeof(wc.sta.password));
  wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  if (use_channel) {
    wc.sta.channel = g_icmp_cache.channel;
  }
  // BSSID is never pinned: roaming must keep working.

  auto const t0 = esp_timer_get_time();
  esp_wifi_set_mode(WIFI_MODE_STA);
  esp_wifi_set_config(WIFI_IF_STA, &wc);
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_start();

  auto const bits =
      xEventGroupWaitBits(g_icmp_eg, kWifiConnectedBit | kWifiFailBit, pdTRUE,
                          pdFALSE, pdMS_TO_TICKS(20000));
  *connect_ms = static_cast<std::uint32_t>((esp_timer_get_time() - t0) / 1000);
  if ((bits & kWifiConnectedBit) == 0) {
    return false;
  }

  if (pre_ms > 0) {
    vTaskDelay(pdMS_TO_TICKS(pre_ms));
  }

  esp_netif_ip_info_t ipi{};
  if (esp_netif_get_ip_info(g_icmp_netif, &ipi) == ESP_OK && ipi.gw.addr != 0) {
    *gateway_be = ipi.gw.addr;
  } else if (use_ip) {
    *gateway_be = g_icmp_cache.gateway;
  }
  return *gateway_be != 0;
}

void CaptureIcmpCache() {
  wifi_ap_record_t ap{};
  esp_netif_ip_info_t ipi{};
  if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
    return;
  }
  if (esp_netif_get_ip_info(g_icmp_netif, &ipi) != ESP_OK) {
    return;
  }
  g_icmp_cache = IcmpCache{};
  g_icmp_cache.channel = ap.primary;
  g_icmp_cache.ip = ipi.ip.addr;
  g_icmp_cache.netmask = ipi.netmask.addr;
  g_icmp_cache.gateway = ipi.gw.addr;
  g_icmp_cache.authmode = static_cast<std::uint8_t>(ap.authmode);
  g_icmp_cache.valid = 1;
}

// One trial: kProbeIcmpBatch reconnects, extended once when borderline.
probe::IcmpTrial RunIcmpTrial(ae::WifiProbeProfile profile,
                              std::uint16_t pre_ms) {
  probe::IcmpTrial trial{};
  auto run_connects = [&](std::uint16_t count) {
    for (std::uint16_t i = 0; i < count; ++i) {
      std::uint32_t connect_ms = 0;
      std::uint32_t gateway = 0;
      if (!IcmpConnect(profile, pre_ms, &connect_ms, &gateway)) {
        probe::IcmpTrialAddConnect(trial, false, connect_ms, 0, 0);
      } else {
        auto const icmp =
            ae::WifiGatewayIcmpProbe(gateway, probe::kProbeIcmpPerConnect);
        probe::IcmpTrialAddConnect(trial, true, connect_ms, icmp.stats.sent,
                                   icmp.stats.received);
      }
      IcmpTeardown();
      vTaskDelay(pdMS_TO_TICKS(250));
    }
  };

  run_connects(probe::kProbeIcmpBatch);
  if (probe::IcmpTrialBorderline(trial)) {
    run_connects(probe::kProbeIcmpExtend);
  }

  std::printf(
      "P_ICMP profile=%d pre=%u connects=%u ok=%u icmp_s=%u icmp_r=%u "
      "loss_ppt=%lu mean_ms=%lu pass=%d\n",
      static_cast<int>(profile), static_cast<unsigned>(pre_ms),
      static_cast<unsigned>(trial.connects),
      static_cast<unsigned>(trial.connect_ok),
      static_cast<unsigned>(trial.icmp_sent),
      static_cast<unsigned>(trial.icmp_recv),
      static_cast<unsigned long>(probe::IcmpTrialLossPpt(trial)),
      static_cast<unsigned long>(probe::IcmpTrialConnectMeanMs(trial)),
      probe::IcmpTrialPasses(trial) ? 1 : 0);
  std::fflush(stdout);
  return trial;
}

[[noreturn]] void RunIcmpSelectStage() {
  SayStage("begin");

  // Seed the cache with a canonical P0 association so the cached-IP and
  // cached-channel profiles have something to reuse.
  std::uint32_t seed_connect_ms = 0;
  std::uint32_t seed_gateway = 0;
  if (IcmpConnect(ae::WifiProbeProfile::kP0Default, 100, &seed_connect_ms,
                  &seed_gateway)) {
    CaptureIcmpCache();
    std::printf("P_ICMP_SEED ch=%u ip=%08lx gw=%08lx auth=%u\n",
                static_cast<unsigned>(g_icmp_cache.channel),
                static_cast<unsigned long>(g_icmp_cache.ip),
                static_cast<unsigned long>(g_icmp_cache.gateway),
                static_cast<unsigned>(g_icmp_cache.authmode));
  } else {
    std::printf("P_ICMP_SEED failed\n");
  }
  std::fflush(stdout);
  IcmpTeardown();
  vTaskDelay(pdMS_TO_TICKS(500));

  auto const pre_table = probe::ProductPreTable();
  auto const post_table = probe::ProductPostTable();
  // Profiles are compared at the first PRE candidate; the PRE search then runs
  // against the winning profile only.
  auto const compare_pre = pre_table.primary[0];
  constexpr auto kProfileCount =
      static_cast<std::size_t>(ae::WifiProbeProfile::kCount);

  probe::IcmpCandidate candidates[kProfileCount]{};
  for (std::size_t i = 0; i < kProfileCount; ++i) {
    candidates[i].profile = static_cast<std::uint8_t>(i);
    candidates[i].measured = true;
    candidates[i].trial =
        RunIcmpTrial(static_cast<ae::WifiProbeProfile>(i), compare_pre);
  }

  auto const winner = probe::SelectIcmpProfile(candidates, kProfileCount);
  if (winner < 0) {
    // Nothing reached the acceptance threshold: fall back to the plain profile
    // with the most conservative PRE the tables offer.
    g_rtc.profile = 0;
    g_rtc.pre_ms = pre_table.extended[pre_table.extended_count - 1];
    std::printf("P_ICMP_WINNER profile=-1 pre=%u fallback=1\n",
                static_cast<unsigned>(g_rtc.pre_ms));
  } else {
    g_rtc.profile = candidates[winner].profile;
    auto const profile = static_cast<ae::WifiProbeProfile>(g_rtc.profile);

    // PRE search: descend 100 → 0, extend upwards only if 100 already fails.
    g_rtc.pre_search = probe::ParamSearchState{};
    while (!probe::ParamSearchFinished(g_rtc.pre_search)) {
      auto const pre = probe::ParamSearchCurrent(pre_table, g_rtc.pre_search);
      auto const trial = RunIcmpTrial(profile, pre);
      probe::ParamSearchRecord(pre_table, g_rtc.pre_search,
                               probe::IcmpTrialPasses(trial));
    }
    g_rtc.pre_ms = probe::ParamSearchFailed(g_rtc.pre_search)
                       ? pre_table.extended[pre_table.extended_count - 1]
                       : probe::ParamSearchSelected(g_rtc.pre_search);

    std::printf("P_ICMP_WINNER profile=%u pre=%u loss_ppt=%lu mean_ms=%lu\n",
                static_cast<unsigned>(g_rtc.profile),
                static_cast<unsigned>(g_rtc.pre_ms),
                static_cast<unsigned long>(probe::IcmpTrialLossPpt(
                    candidates[winner].trial)),
                static_cast<unsigned long>(probe::IcmpTrialConnectMeanMs(
                    candidates[winner].trial)));
  }
  std::fflush(stdout);

  // First POST candidate for the batch stages.
  g_rtc.post_search = probe::ParamSearchState{};
  g_rtc.post_ms = probe::ParamSearchCurrent(post_table, g_rtc.post_search);
  probe::ProductProbeAdvanceStage(g_rtc);
  DeepSleepToNextStage();
}

// ---------------------------------------------------------------------------
// Prepared hot path
// ---------------------------------------------------------------------------

prepared_send::FastPathConfig MakeFastConfig() {
  auto cfg = prepared_send::FastPathConfigForProbeProfile(
      static_cast<ae::WifiProbeProfile>(g_rtc.profile), g_rtc.pre_ms,
      g_rtc.post_ms);
  cfg.use_bssid = false;
  cfg.wifi_storage_ram = true;
  cfg.wifi_nvs_enable = true;
  cfg.auth = prepared_send::FastAuthMode::kWpa2;
  cfg.retry_max = 10;
  // Wait for the TX-done callback, then hold for the selected POST delay.
  cfg.post_mode = prepared_send::FastPostMode::kTxDoneCb;
  cfg.post_delay_ms = g_rtc.post_ms;
  return cfg;
}

void ComputeSleepElapsed() {
  g_sleep_elapsed_us = 0;
  if (static_cast<esp_reset_reason_t>(g_early.reset_reason) !=
      ESP_RST_DEEPSLEEP) {
    return;
  }
  if (g_rtc_sleep_arm_us == 0 ||
      g_early.app_entry_rtc_us < g_rtc_sleep_arm_us) {
    return;
  }
  auto const elapsed = g_early.app_entry_rtc_us - g_rtc_sleep_arm_us;
  g_sleep_elapsed_us = elapsed > 0xffffffffull
                           ? 0xffffffffu
                           : static_cast<std::uint32_t>(elapsed);
}

bool WifiFailedBeforeEncode(prepared_send::FastSendResult const& r) {
  if (r.status == prepared_send::HotSendStatus::kWifiFailed) {
    return true;
  }
  bool const encode_ok =
      (r.status_flags &
       static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk)) != 0;
  return !encode_ok && r.status != prepared_send::HotSendStatus::kSent;
}

// Payload for the next packet of the current batch. Probe batches carry the
// parameter under test; the hot run additionally carries the previous send's
// timing, because the current send's own timing is not known yet.
std::size_t BuildBatchPayload(std::uint16_t seq, std::uint8_t* out,
                              std::size_t capacity) {
  if (Stage() == probe::ProbeStage::kHotRun) {
    probe::HotData msg{};
    msg.session = g_rtc.session;
    msg.batch_id = g_rtc.batch_id;
    msg.parameter_id = g_rtc.parameter_id;
    msg.seq = seq;
    msg.profile = g_rtc.profile;
    msg.pre_ms = g_rtc.pre_ms;
    msg.post_ms = g_rtc.post_ms;
    msg.sleep_ms = g_rtc.sleep_ms;
    msg.prev_seq = g_rtc.prev_seq;
    msg.prev_connect_us = g_rtc.prev_connect_us;
    msg.prev_cycle_us = g_rtc.prev_cycle_us;
    msg.prev_txdone_us = g_rtc.prev_txdone_us;
    msg.prev_sleep_elapsed_us = g_rtc.prev_sleep_elapsed_us;
    msg.prev_status = g_rtc.prev_status;
    msg.prev_valid = g_rtc.prev_valid;
    return probe::Pack(msg, out, capacity);
  }

  probe::ProbeData msg{};
  msg.session = g_rtc.session;
  msg.batch_id = g_rtc.batch_id;
  msg.parameter_id = g_rtc.parameter_id;
  msg.seq = seq;
  msg.stage = g_rtc.stage;
  msg.profile = g_rtc.profile;
  msg.pre_ms = g_rtc.pre_ms;
  msg.post_ms = g_rtc.post_ms;
  // The no-sleep probe batch is measured without any sleep at all.
  msg.sleep_ms = Stage() == probe::ProbeStage::kPostProbe ? 0 : g_rtc.sleep_ms;
  return probe::Pack(msg, out, capacity);
}

// Sends one prepared packet. Returns false when the probe has to restart.
bool SendOnePreparedPacket() {
  if (!prepared_send::PreparedWifiRtcCacheIsValid(g_rtc_wifi_cache)) {
    return false;
  }
  g_wifi_snapshot =
      prepared_send::SnapshotFromPreparedWifiRtcCache(g_rtc_wifi_cache);
  if (!g_wifi_snapshot.valid_ip || g_wifi_snapshot.channel == 0) {
    return false;
  }
  if (!prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() == 0) {
    return false;
  }
  if (g_rtc_batch_attempts >= kMaxBatchAttempts) {
    return false;
  }
  ++g_rtc_batch_attempts;

  auto const seq = probe::ProductProbeNextSeq(g_rtc);
  std::uint8_t buffer[probe::kMaxProbeMessageSize]{};
  auto const size = BuildBatchPayload(seq, buffer, sizeof(buffer));
  if (size == 0) {
    return false;
  }

  auto const cycle_start = esp_timer_get_time();
  auto const result = prepared_send::SendPreparedOnceWithFastPath(
      g_cfg, ToDataBuffer(buffer, size), &g_wifi_snapshot);
  auto const cycle_end = esp_timer_get_time();

  if (result.status == prepared_send::HotSendStatus::kSent) {
    auto const delta = cycle_end - cycle_start;
    probe::ProductProbeRecordHotSend(
        g_rtc, seq, result.connect_us,
        delta < 0 ? 0 : static_cast<std::uint32_t>(delta),
        result.tx_done_wait_us, g_sleep_elapsed_us, 1);
    return true;
  }

  probe::ProductProbeRecordHotFailure(g_rtc);
  if (WifiFailedBeforeEncode(result)) {
    // The nonce was not consumed. The network changed under us, so the
    // selected parameters have to be measured again.
    return false;
  }
  // Encode or send failure after Wi-Fi came up: retry the same slot.
  return true;
}

// Stage 2: the whole batch in one boot, nothing between the packets.
[[noreturn]] void RunPostProbeStage() {
  while (g_rtc.batch_sent < g_rtc.batch_expected) {
    if (!SendOnePreparedPacket()) {
      Reprobe("post_probe_send");
    }
  }
  probe::ProductProbeAdvanceStage(g_rtc);
  DeepSleepToNextStage();
}

// Stages 4 and 7: one packet per boot with a deep sleep in between.
[[noreturn]] void RunSleepingBatchStage() {
  if (g_rtc.batch_sent >= g_rtc.batch_expected) {
    probe::ProductProbeAdvanceStage(g_rtc);
    DeepSleepToNextStage();
  }
  bool const hot = Stage() == probe::ProbeStage::kHotRun;
  if (!SendOnePreparedPacket()) {
    Reprobe(hot ? "hot_send" : "sleep_confirm_send");
  }
  if (g_rtc.batch_sent >= g_rtc.batch_expected) {
    probe::ProductProbeAdvanceStage(g_rtc);
  }
  DeepSleepToNextStage();
}

// ---------------------------------------------------------------------------
// Aether FULL stages
// ---------------------------------------------------------------------------

bool PurposeIsQuery(FullPurpose purpose) {
  return purpose == FullPurpose::kQueryPostBatch ||
         purpose == FullPurpose::kQuerySleepBatch;
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
  g_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}.AdaptersFactory(
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

std::size_t BuildFullPayload(std::uint8_t* out, std::size_t capacity) {
  if (g_purpose == FullPurpose::kHotSummary) {
    probe::HotSummary msg{};
    msg.session = g_rtc.session;
    msg.batch_id = g_rtc.batch_id;
    msg.parameter_id = g_rtc.parameter_id;
    msg.profile = g_rtc.profile;
    msg.pre_ms = g_rtc.pre_ms;
    msg.post_ms = g_rtc.post_ms;
    msg.sleep_ms = g_rtc.sleep_ms;
    msg.hot_sent = g_rtc.hot_sent;
    msg.hot_fail = g_rtc.hot_fail;
    msg.reprobe_count = g_rtc.reprobe_count;
    return probe::Pack(msg, out, capacity);
  }
  if (PurposeIsQuery(g_purpose)) {
    probe::ProbeQuery msg{};
    msg.session = g_rtc.session;
    msg.batch_id = g_rtc.batch_id;
    msg.parameter_id = g_rtc.parameter_id;
    msg.expected = g_rtc.batch_expected;
    return probe::Pack(msg, out, capacity);
  }
  // Stage markers: FULL_PREPARE and PROBE_COMPLETE.
  probe::ProbeData msg{};
  msg.session = g_rtc.session;
  msg.batch_id = g_rtc.batch_id;
  msg.parameter_id = g_rtc.parameter_id;
  msg.seq = 0;
  msg.stage = g_rtc.stage;
  msg.profile = g_rtc.profile;
  msg.pre_ms = g_rtc.pre_ms;
  msg.post_ms = g_rtc.post_ms;
  msg.sleep_ms = g_rtc.sleep_ms;
  return probe::Pack(msg, out, capacity);
}

void WriteFullPayload() {
  std::uint8_t buffer[probe::kMaxProbeMessageSize]{};
  auto const size = BuildFullPayload(buffer, sizeof(buffer));
  if (size == 0) {
    g_app->Exit(1);
    return;
  }
  g_write_status_known = false;
  auto& wa = g_stream->Write(ToDataBuffer(buffer, size));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    if (st == ae::WriteAction::Status::kSuccess) {
      g_write_ok = true;
      g_write_status_known = true;
    } else if (st == ae::WriteAction::Status::kFail) {
      g_write_ok = false;
      g_write_status_known = true;
    }
  });
}

void OnFullData(ae::DataBuffer const& data) {
  probe::ProbeResult result{};
  if (!probe::Unpack(data.data(), data.size(), result)) {
    return;
  }
  if (result.session != g_rtc.session || result.batch_id != g_rtc.batch_id) {
    return;
  }
  g_query_unique = result.unique;
  g_query_have_result = true;
  std::printf("P_QUERY_RESULT batch=%u expected=%u unique=%u dup=%u miss=%u\n",
              static_cast<unsigned>(result.batch_id),
              static_cast<unsigned>(result.expected),
              static_cast<unsigned>(result.unique),
              static_cast<unsigned>(result.dup),
              static_cast<unsigned>(result.missing));
  std::fflush(stdout);
}

void MaybeStartFullWork() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  g_write_armed = true;
  if (PurposeIsQuery(g_purpose)) {
    probe::LateQueryStart(g_query, NowMs(), g_rtc.batch_expected);
  }
  WriteFullPayload();
}

void OnFullClientReady(ae::Client::ptr client_ptr) {
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
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                            ae::P2pPortHandle{});
  g_data_sub = g_stream->out_data_event().Subscribe(
      [](ae::DataBuffer const& data) { OnFullData(data); });
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeStartFullWork(); });
  MaybeStartFullWork();
}

void StartFullStage(FullPurpose purpose) {
  g_purpose = purpose;
  g_write_armed = false;
  g_write_ok = false;
  g_write_status_known = false;
  g_stage_ok = false;
  g_finished_in_loop = false;
  g_query_have_result = false;
  g_query_finished = false;
  g_query_timed_out = false;
  g_query_unique = 0;
  g_full_start_ms = NowMs();
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

// Re-issues the query while packets may still be in flight.
void PumpQuery() {
  if (!g_write_armed || g_query_finished) {
    return;
  }
  auto const now = NowMs();
  if (g_query_have_result) {
    g_query_have_result = false;
    auto const action = probe::LateQueryOnResult(g_query, now, g_query_unique);
    if (action == probe::LateQueryAction::kQueryAgain) {
      WriteFullPayload();
      return;
    }
    g_query_timed_out = (action == probe::LateQueryAction::kTimeout);
    g_query_finished = true;
    return;
  }
  if (probe::LateQueryOnTick(g_query, now) ==
      probe::LateQueryAction::kTimeout) {
    g_query_timed_out = true;
    g_query_finished = true;
  }
}

bool ExportBlockForNextBatch(std::uint16_t reserve) {
  if (!prepared_send::CapturePreparedWifiRtcCache(&g_rtc_wifi_cache)) {
    return false;
  }
  if (!prepared_send::ExportPreparedSendBlock(g_client, kServiceUid, reserve)) {
    return false;
  }
  return prepared_send::HasPreparedSendBlock() &&
         prepared_send::PreparedMessageLeft() ==
             static_cast<std::uint32_t>(reserve);
}

// Sets up the next prepared batch of `expected` packets at the current POST.
bool BeginPreparedStage(probe::ProbeStage stage, std::uint16_t expected) {
  g_cfg = MakeFastConfig();
  if (!ExportBlockForNextBatch(expected)) {
    return false;
  }
  if (stage == probe::ProbeStage::kHotRun) {
    // Probe batches share the send counters; the summary must report the
    // production run only.
    g_rtc.hot_sent = 0;
    g_rtc.hot_fail = 0;
  }
  probe::ProductProbeBeginBatch(g_rtc, g_rtc.post_ms, expected);
  g_rtc_batch_attempts = 0;
  probe::ProductProbeSetStage(g_rtc, stage);
  return true;
}

// Judges the batch just queried and decides what the next boot does.
bool ApplyQueryVerdict() {
  auto const table = probe::ProductPostTable();
  auto const verdict = probe::JudgeBatch(g_query.last_unique,
                                        g_rtc.batch_expected,
                                        g_rtc.extra_batch_used != 0);
  bool const sleep_stage = g_purpose == FullPurpose::kQuerySleepBatch;
  auto const probe_stage = sleep_stage ? probe::ProbeStage::kSleepConfirm
                                       : probe::ProbeStage::kPostProbe;

  std::printf(
      "P_VERDICT stage=%u post=%u unique=%u expected=%u verdict=%u batches=%u "
      "timeout=%d\n",
      static_cast<unsigned>(g_rtc.stage), static_cast<unsigned>(g_rtc.post_ms),
      static_cast<unsigned>(g_query.last_unique),
      static_cast<unsigned>(g_rtc.batch_expected),
      static_cast<unsigned>(verdict),
      static_cast<unsigned>(g_rtc_stage_batches), g_query_timed_out ? 1 : 0);
  std::fflush(stdout);

  ++g_rtc_stage_batches;

  // A near miss buys exactly one more batch at the same POST value.
  if (verdict == probe::BatchVerdict::kExtraBatch &&
      g_rtc_stage_batches < kMaxProbeBatchesPerStage) {
    g_rtc.extra_batch_used = 1;
    return BeginPreparedStage(probe_stage, probe::kProbeBatchSize);
  }
  g_rtc.extra_batch_used = 0;

  if (sleep_stage) {
    // The POST delay is already settled; a failing sleep confirmation only
    // means the sleeping hot path needs more slack than the no-sleep probe.
    // kProbeComplete is an Aether stage, so no prepared block is exported here.
    auto const accept = [&]() {
      g_rtc_stage_batches = 0;
      probe::ProductProbeSetStage(g_rtc, probe::ProbeStage::kProbeComplete);
      return true;
    };
    if (verdict == probe::BatchVerdict::kPass ||
        g_rtc_stage_batches >= kMaxProbeBatchesPerStage) {
      return accept();
    }
    auto const raised = RaisePost(g_rtc.post_ms);
    if (raised == g_rtc.post_ms) {
      return accept();
    }
    g_rtc.post_ms = raised;
    return BeginPreparedStage(probe::ProbeStage::kSleepConfirm,
                              probe::kProbeBatchSize);
  }

  probe::ParamSearchRecord(table, g_rtc.post_search,
                           verdict == probe::BatchVerdict::kPass);
  bool const search_done = probe::ParamSearchFinished(g_rtc.post_search);
  if (search_done) {
    g_rtc.post_ms = probe::ParamSearchFailed(g_rtc.post_search)
                        ? table.extended[table.extended_count - 1]
                        : probe::ParamSearchSelected(g_rtc.post_search);
  } else {
    g_rtc.post_ms = probe::ParamSearchCurrent(table, g_rtc.post_search);
  }

  // The search is over and the winning value already delivered a full batch:
  // move to the sleeping confirmation of the same value.
  if (search_done && verdict == probe::BatchVerdict::kPass) {
    g_rtc_stage_batches = 0;
    return BeginPreparedStage(probe::ProbeStage::kSleepConfirm,
                              probe::kProbeBatchSize);
  }
  if (g_rtc_stage_batches >= kMaxProbeBatchesPerStage) {
    g_rtc_stage_batches = 0;
    return BeginPreparedStage(probe::ProbeStage::kSleepConfirm,
                              probe::kProbeBatchSize);
  }
  return BeginPreparedStage(probe_stage, probe::kProbeBatchSize);
}

// Runs while the Aether app is still alive, so prepared export and Save work.
void FinishWorkInLoop() {
  bool ok = false;
  switch (g_purpose) {
    case FullPurpose::kPrepareBatch:
      ok = g_write_ok && BeginPreparedStage(probe::ProbeStage::kPostProbe,
                                            probe::kProbeBatchSize);
      break;
    case FullPurpose::kProbeComplete:
      ok = g_write_ok && BeginPreparedStage(probe::ProbeStage::kHotRun,
                                            probe::kProbeHotCount);
      break;
    case FullPurpose::kQueryPostBatch:
    case FullPurpose::kQuerySleepBatch:
      ok = ApplyQueryVerdict();
      break;
    case FullPurpose::kHotSummary:
      if (g_write_ok) {
        probe::ProductProbeAdvanceStage(g_rtc);
        ok = true;
      }
      break;
  }
  if (ok) {
    g_app->aether().Save();
  }
  g_stage_ok = ok;
  g_finished_in_loop = true;
  g_app->Exit(ok ? 0 : 1);
}

[[noreturn]] void EndFullStage() {
  bool const ok = g_stage_ok;
  ReleaseApp();
  prepared_send::ReleaseFullAetherWifiForHotPath();
  if (!ok) {
    Reprobe("full_stage");
  }
  SayStage("full_done");
  DeepSleepToNextStage();
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

// Only a deep-sleep timer wake continues the run; everything else is cold.
bool IsColdBoot() {
  if (static_cast<esp_reset_reason_t>(g_early.reset_reason) !=
      ESP_RST_DEEPSLEEP) {
    return true;
  }
  return static_cast<esp_sleep_wakeup_cause_t>(g_early.wakeup_cause) !=
         ESP_SLEEP_WAKEUP_TIMER;
}

void PrepareOnBoot() {
  g_early = GetExperimentEarlyEntrySnapshot();
  ComputeSleepElapsed();
  if (IsColdBoot() || g_rtc.stage >= probe::kProbeStageCount) {
    g_rtc_wifi_cache = prepared_send::PreparedWifiRtcCache{};
    g_icmp_cache = IcmpCache{};
    g_rtc_batch_attempts = 0;
    g_rtc_stage_batches = 0;
    probe::ProductProbeColdBootReset(g_rtc, esp_random());
  }
  g_cfg = MakeFastConfig();
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

  auto const stage = Stage();
  if (stage == probe::ProbeStage::kIcmpSelect) {
    RunIcmpSelectStage();
  }
  if (stage == probe::ProbeStage::kFullPrepare) {
    SayStage("begin");
    StartFullStage(FullPurpose::kPrepareBatch);
    return;
  }
  if (stage == probe::ProbeStage::kPostQuery) {
    SayStage("begin");
    StartFullStage(FullPurpose::kQueryPostBatch);
    return;
  }
  if (stage == probe::ProbeStage::kSleepConfirmQuery) {
    SayStage("begin");
    StartFullStage(FullPurpose::kQuerySleepBatch);
    return;
  }
  if (stage == probe::ProbeStage::kProbeComplete) {
    SayStage("begin");
    StartFullStage(FullPurpose::kProbeComplete);
    return;
  }
  if (stage == probe::ProbeStage::kHotSummary) {
    SayStage("begin");
    StartFullStage(FullPurpose::kHotSummary);
    return;
  }
  if (stage == probe::ProbeStage::kDone) {
    SayStage("done");
    g_idle = true;
    return;
  }
  // Prepared batch stages run synchronously from loop().
}

void loop() {
  using namespace temp_sensor;  // NOLINT
  if (g_idle) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    return;
  }

  auto const stage = Stage();
  if (stage == probe::ProbeStage::kPostProbe) {
    RunPostProbeStage();
  }
  if (stage == probe::ProbeStage::kSleepConfirm ||
      stage == probe::ProbeStage::kHotRun) {
    RunSleepingBatchStage();
  }

  if (!g_app) {
    Reprobe("no_app");
  }

  if (!g_app->IsExited()) {
    auto const next_time = g_app->Update(ae::Now());
    if (PurposeIsQuery(g_purpose)) {
      PumpQuery();
    }
    bool const work_done = PurposeIsQuery(g_purpose)
                               ? g_query_finished
                               : (g_write_armed && g_write_status_known);
    if (work_done && !g_finished_in_loop) {
      FinishWorkInLoop();
      return;
    }
    if ((NowMs() - g_full_start_ms) > kFullStageTimeoutMs) {
      g_stage_ok = false;
      g_app->Exit(1);
      return;
    }
    if (!g_app->IsExited()) {
      g_app->WaitUntil(next_time);
    }
    return;
  }

  EndFullStage();
}

#else

void setup() {}
void loop() {}

#endif
