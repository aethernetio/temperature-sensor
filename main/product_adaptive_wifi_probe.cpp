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
 * The measured stages send exactly one datagram per wake and then enter a real
 * timer deep sleep, which is how production sends behave. A software restart is
 * never a substitute for that sleep: a sample whose sleep the following boot
 * cannot confirm is discarded together with its whole batch.
 *
 *   0 ICMP_SELECT              raw Wi-Fi, pick profile then PRE delay
 *   1 FULL_PREPARE_POST_BATCH  Aether FULL: marker + prepared block
 *   2 POST_PROBE_SLEEP250      one prepared send per wake, deep sleep between
 *   3 POST_QUERY               Aether FULL: query the batch, judge the POST
 *   4 HOT_PREPARE              Aether FULL: marker + prepared block for HOT
 *   5 PPK_ARM                  audible arm marker, wait for the power logger
 *   6 HOT_RUN                  production sends, one per wake, deep sleep
 *   7 HOT_SUMMARY              Aether FULL: totals
 *   8 DONE                     idle
 *
 * Stages 1, 3, 4 and 7 run the full Aether stack and hand over with a software
 * restart, which preserves RTC state; entering deep sleep straight after that
 * teardown panics the chip. Stages 2, 5 and 6 hand over with a real timer deep
 * sleep. Any other reset restarts at stage 0, which is also what a pre-Encode
 * Wi-Fi failure on the hot path forces: the network may have changed, so the
 * selected parameters are no longer trusted.
 *
 * Stages 2 and 6 are measured and must not print.
 */

#include <cassert>
#include <chrono>
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

// Non-zero runs the deep-sleep smoke instead of the campaign: no ICMP search,
// fixed P1/PRE 0/POST 300, this many measured sends, then stop. It exists to
// prove that the measured path really deep sleeps before any long run starts.
#ifndef AE_PRODUCT_PROBE_SMOKE
#  define AE_PRODUCT_PROBE_SMOKE 0
#endif
static constexpr std::uint16_t kSmokePackets = AE_PRODUCT_PROBE_SMOKE;
static constexpr bool kSmokeMode = kSmokePackets > 0;

// How long PPK_ARM stays awake so the campaign runner can attach its power
// logger before the first production packet.
#ifndef AE_PRODUCT_PROBE_PPK_ARM_MS
#  define AE_PRODUCT_PROBE_PPK_ARM_MS 20000
#endif
static constexpr std::uint32_t kPpkArmMs = AE_PRODUCT_PROBE_PPK_ARM_MS;

// The measured deep sleep, and the sleep every stage handover uses when it can
// sleep at all.
static constexpr std::uint32_t kStageSleepUs =
    static_cast<std::uint32_t>(probe::kProbeSleepMs) * 1000u;

// Prepared attempts allowed inside one batch before the probe restarts.
static constexpr std::uint16_t kMaxBatchAttempts =
    static_cast<std::uint16_t>(probe::kProbeHotCount + 40);

// Total probe batches across the whole POST search. The descending table needs
// at most two batches per candidate, so exceeding this means something outside
// the algorithm is wrong and the path is declared invalid rather than guessed.
static constexpr std::uint16_t kMaxProbeBatches = 24;

// Wall clock budget for one Aether FULL boot.
static constexpr std::uint32_t kFullStageTimeoutMs = 60000;

#if defined(ESP_PLATFORM)

// Which piece of work the current Aether FULL boot performs.
enum class FullPurpose : std::uint8_t {
  kPreparePostBatch,
  kQueryPostBatch,
  kPrepareHot,
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

// RTC_DATA_ATTR only survives deep sleep; the bootloader reloads it on a
// software restart. The stage machine has to survive both, so the state is kept
// uninitialised and cleared explicitly whenever the boot is cold.
RTC_NOINIT_ATTR probe::ProbeRtcState g_rtc;
RTC_NOINIT_ATTR prepared_send::PreparedWifiRtcCache g_rtc_wifi_cache;
RTC_NOINIT_ATTR IcmpCache g_icmp_cache;
RTC_NOINIT_ATTR std::uint16_t g_rtc_batch_attempts;
RTC_NOINIT_ATTR std::uint16_t g_rtc_total_batches;
RTC_NOINIT_ATTR std::uint64_t g_rtc_sleep_arm_us;
// Deep sleep can be rejected. A measured stage cannot print, so the counts are
// carried in RTC and reported once the run is audible again.
RTC_NOINIT_ATTR std::uint16_t g_rtc_sleep_reject;
RTC_NOINIT_ATTR std::int16_t g_rtc_sleep_reject_err;
RTC_NOINIT_ATTR std::uint16_t g_rtc_timer_wakes;
RTC_NOINIT_ATTR std::uint16_t g_rtc_bad_wakes;

// Boot history, for the same reason: each boot records how it woke and the next
// audible stage reports the whole trail.
struct BootMark {
  std::uint8_t stage;
  std::uint8_t reset_reason;
  std::uint8_t wakeup_cause;
  std::uint8_t cold;
};
static constexpr std::uint8_t kBootMarkCount = 16;
RTC_NOINIT_ATTR BootMark g_rtc_boot_marks[kBootMarkCount];
RTC_NOINIT_ATTR std::uint8_t g_rtc_boot_mark_count;

static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

ExperimentEarlyEntrySnapshot g_early{};
std::uint32_t g_sleep_elapsed_us = 0;
std::uint32_t g_wake_overhead_us = 0;
bool g_woke_from_timer_deep_sleep = false;

std::shared_ptr<ae::AetherApp> g_app;
ae::Client::ptr g_client;
std::unique_ptr<ae::P2pStream> g_stream;
ae::Subscription g_select_sub;
ae::Subscription g_stream_sub;
ae::Subscription g_data_sub;
ae::Subscription g_write_sub;

FullPurpose g_purpose{FullPurpose::kPreparePostBatch};
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
  return probe::ProbeStageIsMeasured(stage);
}

std::uint16_t BatchSize() {
  return kSmokeMode ? kSmokePackets : probe::kProbeBatchSize;
}

void ReportBootMarks() {
  if (g_rtc_boot_mark_count == 0) {
    return;
  }
  auto const count = g_rtc_boot_mark_count > kBootMarkCount
                         ? kBootMarkCount
                         : g_rtc_boot_mark_count;
  for (std::uint8_t i = 0; i < count; ++i) {
    auto const& m = g_rtc_boot_marks[i];
    std::printf("P_BOOT stage=%u reset=%u wake=%u cold=%u\n",
                static_cast<unsigned>(m.stage),
                static_cast<unsigned>(m.reset_reason),
                static_cast<unsigned>(m.wakeup_cause),
                static_cast<unsigned>(m.cold));
  }
  std::printf(
      "P_BOOT_SUM boots=%u timer_wakes=%u bad_wakes=%u reject=%u "
      "reject_err=%d sleep_us=%lu overhead_us=%lu\n",
      static_cast<unsigned>(g_rtc_boot_mark_count),
      static_cast<unsigned>(g_rtc_timer_wakes),
      static_cast<unsigned>(g_rtc_bad_wakes),
      static_cast<unsigned>(g_rtc_sleep_reject),
      static_cast<int>(g_rtc_sleep_reject_err),
      static_cast<unsigned long>(g_sleep_elapsed_us),
      static_cast<unsigned long>(g_wake_overhead_us));
  std::fflush(stdout);
  g_rtc_boot_mark_count = 0;
}

void SayStage(char const* tag) {
  if (StageIsMeasured(Stage())) {
    return;
  }
  ReportBootMarks();
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

// Stage handover for the boots that cannot sleep: entering deep sleep straight
// after an Aether or raw Wi-Fi teardown panics the chip. RTC state survives a
// restart, so the run continues at the next stage either way. This is never
// used to end a measured send.
[[noreturn]] void RestartToNextStage() {
  assert(!StageIsMeasured(Stage()) &&
         "measured stages must hand over with a real deep sleep");
  std::printf("P_RESTART stage=%u\n", static_cast<unsigned>(g_rtc.stage));
  std::fflush(stdout);
  g_rtc_sleep_arm_us = 0;
  esp_restart();
  for (;;) {
  }
}

// The real thing: RTC domains held up, wake armed on the timer, and no restart
// fallback that could be mistaken for a sleep. Everything the measured stages
// report about sleeping comes from here.
[[noreturn]] void DeepSleepToNextStage() {
  // The parked sample was awake for exactly this long. Boots without a sample
  // must not overwrite the figure belonging to the last one that had it.
  if (probe::ProductProbeHasPendingSample(g_rtc)) {
    auto const awake = esp_timer_get_time();
    g_rtc.prev.awake_us = awake < 0 ? 0 : static_cast<std::uint32_t>(awake);
  }

  esp_sleep_enable_timer_wakeup(kStageSleepUs);
  g_rtc_sleep_arm_us = esp_rtc_get_time_us();
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_ON);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_ON);
#  endif
  bool const audible = !StageIsMeasured(Stage());
  if (audible) {
    std::printf("P_SLEEP stage=%u us=%lu\n",
                static_cast<unsigned>(g_rtc.stage),
                static_cast<unsigned long>(kStageSleepUs));
    std::fflush(stdout);
  }
  auto const err = esp_deep_sleep_try_to_start();

  // The request was refused. Record it and retry with the variant that cannot
  // be rejected. The next boot sees a reset reason other than a timer wake and
  // throws the batch away, so a refused sleep can never become a sample.
  if (g_rtc_sleep_reject < 0xffff) {
    ++g_rtc_sleep_reject;
  }
  g_rtc_sleep_reject_err = static_cast<std::int16_t>(err);
  if (audible) {
    std::printf("P_SLEEP_REJECT err=%d\n", static_cast<int>(err));
    std::fflush(stdout);
  }
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
  g_rtc_total_batches = 0;
  probe::ProductProbeFailureResetStage(g_rtc);
  std::printf("P_REPROBE reason=%s count=%u\n", reason,
              static_cast<unsigned>(g_rtc.reprobe_count));
  std::fflush(stdout);
  RestartToNextStage();
}

ae::DataBuffer ToDataBuffer(std::uint8_t const* data, std::size_t size) {
  return ae::DataBuffer{data, data + size};
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

// The smoke run measures the sleep path, not the network, so it skips the
// search entirely and pins the parameters it was asked for.
[[noreturn]] void RunSmokeSelectStage() {
  g_rtc.profile = static_cast<std::uint8_t>(ae::WifiProbeProfile::kP1CachedIp);
  g_rtc.pre_ms = 0;
  g_rtc.post_search = probe::PostSearchState{};
  g_rtc.post_ms = probe::PostSearchCurrent(g_rtc.post_search);
  std::printf("P_SMOKE packets=%u profile=%u pre=%u post=%u sleep=%u\n",
              static_cast<unsigned>(kSmokePackets),
              static_cast<unsigned>(g_rtc.profile),
              static_cast<unsigned>(g_rtc.pre_ms),
              static_cast<unsigned>(g_rtc.post_ms),
              static_cast<unsigned>(g_rtc.sleep_ms));
  std::fflush(stdout);
  probe::ProductProbeAdvanceStage(g_rtc);
  RestartToNextStage();
}

[[noreturn]] void RunIcmpSelectStage() {
  SayStage("begin");
  if (kSmokeMode) {
    RunSmokeSelectStage();
  }

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

  // The POST search always starts at the most conservative candidate.
  g_rtc.post_search = probe::PostSearchState{};
  g_rtc.post_ms = probe::PostSearchCurrent(g_rtc.post_search);
  probe::ProductProbeAdvanceStage(g_rtc);
  RestartToNextStage();
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
  // Hold for the selected POST delay measured from the TX-done success of this
  // datagram. Waiting for the first callback of any status would accept a
  // failed transmission, and the 5 ms observe window of kFirstSuccess is a lab
  // diagnostic that production must not pay for.
  cfg.post_mode = prepared_send::FastPostMode::kTxDoneCb;
  cfg.tx_done_wait = prepared_send::FastTxDoneWaitMode::kFirstSuccessNoObserve;
  cfg.post_delay_ms = g_rtc.post_ms;
  return cfg;
}

void ComputeWakeMetrics() {
  g_sleep_elapsed_us = 0;
  g_wake_overhead_us = 0;
  g_woke_from_timer_deep_sleep =
      static_cast<esp_reset_reason_t>(g_early.reset_reason) ==
          ESP_RST_DEEPSLEEP &&
      static_cast<esp_sleep_wakeup_cause_t>(g_early.wakeup_cause) ==
          ESP_SLEEP_WAKEUP_TIMER;
  if (!g_woke_from_timer_deep_sleep) {
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
  if (g_sleep_elapsed_us > kStageSleepUs) {
    g_wake_overhead_us = g_sleep_elapsed_us - kStageSleepUs;
  }
}

bool WifiFailedBeforeEncode(prepared_send::FastSendResult const& r) {
  if (r.status == prepared_send::HotSendStatus::kWifiFailed) {
    return true;
  }
  bool const encode_ok =
      (r.status_flags &
       static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk)) != 0;
  return !encode_ok && !prepared_send::HotSendConsumedNonce(r.status);
}

// Payload for the next packet of the current batch. The POST probe and the hot
// run send the same message so the delay is selected with exactly the packet
// production uses. It carries the previous send's timing because the current
// send's own cost is only known after it is over.
std::size_t BuildBatchPayload(std::uint16_t seq, std::uint8_t* out,
                              std::size_t capacity) {
  probe::HotData msg{};
  msg.session = g_rtc.session;
  msg.batch_id = g_rtc.batch_id;
  msg.parameter_id = g_rtc.parameter_id;
  msg.seq = seq;
  msg.stage = g_rtc.stage;
  msg.profile = g_rtc.profile;
  msg.pre_ms = g_rtc.pre_ms;
  msg.post_ms = g_rtc.post_ms;
  msg.sleep_ms = g_rtc.sleep_ms;

  auto const& prev = g_rtc.prev;
  msg.prev_seq = prev.seq;
  msg.prev_status = prev.status;
  msg.prev_flags = prev.flags;
  msg.prev_connect_us = prev.connect_us;
  msg.prev_cycle_us = prev.cycle_us;
  msg.prev_encode_us = prev.encode_us;
  msg.prev_sendto_call_us = prev.sendto_call_us;
  msg.prev_send_to_txdone_us = prev.send_to_txdone_us;
  msg.prev_txdone_minus_sendto_return_us = prev.txdone_minus_sendto_return_us;
  msg.prev_actual_post_us = prev.actual_post_us;
  msg.prev_teardown_us = prev.teardown_us;
  msg.prev_awake_us = prev.awake_us;
  msg.prev_sleep_elapsed_us = prev.sleep_elapsed_us;
  msg.prev_wake_overhead_us = prev.wake_overhead_us;
  return probe::Pack(msg, out, capacity);
}

// Sends one prepared packet and parks it as the pending sample. Returns false
// when the probe has to restart, which only happens while the prepared nonce is
// still untouched.
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
  (void)probe::ProductProbeNextGeneration(g_rtc);
  std::uint8_t buffer[probe::kMaxProbeMessageSize]{};
  auto const size = BuildBatchPayload(seq, buffer, sizeof(buffer));
  if (size == 0) {
    return false;
  }

  auto const cycle_start = esp_timer_get_time();
  auto const result = prepared_send::SendPreparedOnceWithFastPath(
      g_cfg, ToDataBuffer(buffer, size), &g_wifi_snapshot);
  auto const cycle_end = esp_timer_get_time();

  if (WifiFailedBeforeEncode(result)) {
    // The nonce was not consumed. The network changed under us, so the selected
    // parameters have to be measured again.
    return false;
  }

  // Everything below consumed a prepared nonce, so the slot is spent whatever
  // happened afterwards and the packet must never be resent.
  probe::ProbeSendTiming timing{};
  timing.seq = seq;
  timing.connect_us = result.connect_us;
  auto const delta = cycle_end - cycle_start;
  timing.cycle_us = delta < 0 ? 0 : static_cast<std::uint32_t>(delta);
  timing.encode_us = result.encode_us;
  timing.sendto_call_us = result.sendto_call_us;
  timing.send_to_txdone_us = result.send_to_txdone_us;
  timing.txdone_minus_sendto_return_us = result.txdone_minus_sendto_return_us;
  timing.actual_post_us = result.actual_post_us;
  timing.teardown_us = result.teardown_us;
  timing.flags = probe::ProbeSampleSet(
      timing.flags, probe::ProbeSampleFlag::kSendtoOk, result.sendto_ok != 0);
  timing.flags = probe::ProbeSampleSet(
      timing.flags, probe::ProbeSampleFlag::kTxDoneConfirmed,
      result.tx_done_confirmed != 0 && result.tx_cb_register_failed == 0);

  switch (result.status) {
    case prepared_send::HotSendStatus::kSent:
      timing.status = 1;
      break;
    case prepared_send::HotSendStatus::kSentTxUnconfirmed:
      // Counted as unconfirmed at commit time, not as a send failure: the
      // datagram did leave the socket.
      timing.status = 2;
      break;
    default:
      timing.status = 0;
      probe::ProductProbeRecordHotFailure(g_rtc);
      break;
  }

  probe::ProductProbeParkSample(g_rtc, timing);
  return true;
}

// Stages 2 and 6. One datagram per wake, one real deep sleep per datagram, and
// a sample only joins the batch once the boot after it proves that the sleep
// happened.

// The batch was not measured the way production sends, so it is thrown away
// whole. Restarting it needs a fresh prepared block, because the discarded
// packets already spent nonces out of the current one.
[[noreturn]] void RestartBatchWithNewBlock(char const* reason) {
  bool const hot = Stage() == probe::ProbeStage::kHotRun;
  if (g_rtc_bad_wakes < 0xffff) {
    ++g_rtc_bad_wakes;
  }
  probe::ProductProbeInvalidateBatch(g_rtc);
  if (probe::ProductProbeBatchInvalidationsExhausted(g_rtc)) {
    Reprobe(reason);
  }
  g_rtc_batch_attempts = 0;
  probe::ProductProbeSetStage(g_rtc,
                              hot ? probe::ProbeStage::kHotPrepare
                                  : probe::ProbeStage::kFullPreparePostBatch);
  RestartToNextStage();
}

[[noreturn]] void RunMeasuredBatchStage() {
  bool const hot = Stage() == probe::ProbeStage::kHotRun;

  if (probe::ProductProbeHasPendingSample(g_rtc)) {
    g_rtc.prev.sleep_elapsed_us = g_sleep_elapsed_us;
    g_rtc.prev.wake_overhead_us = g_wake_overhead_us;
    if (!probe::ProductProbeCommitPendingSample(g_rtc,
                                                g_woke_from_timer_deep_sleep)) {
      RestartBatchWithNewBlock(hot ? "hot_sleep_unconfirmed"
                                   : "post_sleep_unconfirmed");
    }
  } else if (g_rtc.batch_armed == 0) {
    // The batch is entered from a software restart, so the first send would
    // have no deep sleep in front of it. Spend one boot on that sleep.
    g_rtc.batch_armed = 1;
    DeepSleepToNextStage();
  } else if (!g_woke_from_timer_deep_sleep) {
    RestartBatchWithNewBlock(hot ? "hot_arm_unconfirmed"
                                 : "post_arm_unconfirmed");
  }

  if (g_rtc.batch_sent >= g_rtc.batch_expected) {
    // The last sample's sleep was confirmed by this very boot, so the handover
    // to the next Aether stage may restart.
    probe::ProductProbeAdvanceStage(g_rtc);
    RestartToNextStage();
  }

  if (!SendOnePreparedPacket()) {
    Reprobe(hot ? "hot_send" : "post_send");
  }
  DeepSleepToNextStage();
}

// ---------------------------------------------------------------------------
// Stage 5: arm the power logger
// ---------------------------------------------------------------------------

// The hot run is silent, so this is the only announcement the campaign runner
// gets. It waits here long enough for the runner to release the PPK2 hold and
// bring its logger up before the first production packet leaves.
[[noreturn]] void RunPpkArmStage() {
  SayStage("begin");
  std::printf(
      "P_HOT_ARM session=%08lx profile=%u pre=%u post=%u sleep=%u count=%u "
      "wait_ms=%lu\n",
      static_cast<unsigned long>(g_rtc.session),
      static_cast<unsigned>(g_rtc.profile), static_cast<unsigned>(g_rtc.pre_ms),
      static_cast<unsigned>(g_rtc.post_ms),
      static_cast<unsigned>(g_rtc.sleep_ms),
      static_cast<unsigned>(g_rtc.batch_expected),
      static_cast<unsigned long>(kPpkArmMs));
  std::fflush(stdout);

  vTaskDelay(pdMS_TO_TICKS(kPpkArmMs));

  std::printf("P_HOT_ARM_GO\n");
  std::fflush(stdout);

  probe::ProductProbeSetStage(g_rtc, probe::ProbeStage::kHotRun);
  // This sleep is the hot run's arming sleep, so the first production packet is
  // preceded by exactly the same deep sleep as every other one.
  g_rtc.batch_armed = 1;
  DeepSleepToNextStage();
}

// ---------------------------------------------------------------------------
// Aether FULL stages
// ---------------------------------------------------------------------------

bool PurposeIsQuery(FullPurpose purpose) {
  return purpose == FullPurpose::kQueryPostBatch;
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
    msg.hot_unconfirmed = g_rtc.hot_unconfirmed;
    msg.reprobe_count = g_rtc.reprobe_count;
    msg.batch_invalidations = g_rtc.batch_invalidations;
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
  // Stage markers: FULL_PREPARE_POST_BATCH and HOT_PREPARE.
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

// Sets up the next prepared batch of `expected` packets at the current POST and
// leaves the run at `next_stage`.
bool BeginPreparedBatch(probe::ProbeStage next_stage, std::uint16_t expected) {
  g_cfg = MakeFastConfig();
  if (!ExportBlockForNextBatch(expected)) {
    return false;
  }
  if (next_stage == probe::ProbeStage::kPpkArm) {
    // Probe batches share the send counters; the summary must report the
    // production run only.
    g_rtc.hot_sent = 0;
    g_rtc.hot_fail = 0;
    g_rtc.hot_unconfirmed = 0;
  }
  probe::ProductProbeBeginBatch(g_rtc, g_rtc.post_ms, expected);
  g_rtc_batch_attempts = 0;
  if (g_rtc_total_batches < 0xffff) {
    ++g_rtc_total_batches;
  }
  probe::ProductProbeSetStage(g_rtc, next_stage);
  return true;
}

// Judges the batch just queried and decides what the next boot does. No branch
// here can turn a failing measurement into a selected POST delay.
bool ApplyPostVerdict() {
  auto stats = probe::ProductProbeBatchStats(g_rtc, g_query.last_unique);
  auto const action = probe::PostSearchRecordBatch(g_rtc.post_search, stats);

  std::printf(
      "P_VERDICT post=%u unique=%u local_ok=%u expected=%u action=%u "
      "batches=%u invalidations=%u timeout=%d\n",
      static_cast<unsigned>(g_rtc.post_ms), static_cast<unsigned>(stats.unique),
      static_cast<unsigned>(stats.local_ok),
      static_cast<unsigned>(stats.expected), static_cast<unsigned>(action),
      static_cast<unsigned>(g_rtc_total_batches),
      static_cast<unsigned>(g_rtc.batch_invalidations),
      g_query_timed_out ? 1 : 0);
  std::fflush(stdout);

  auto const finish_invalid = [](char const* reason) {
    std::printf("P_PATH_INVALID reason=%s post=%u\n", reason,
                static_cast<unsigned>(g_rtc.post_ms));
    std::fflush(stdout);
    // No hot run and no power capture: there is no POST delay this network
    // supports, so there is nothing to demonstrate.
    probe::ProductProbeSetStage(g_rtc, probe::ProbeStage::kDone);
    return true;
  };

  if (g_rtc_total_batches >= kMaxProbeBatches &&
      action != probe::PostSearchAction::kFinishedSelected) {
    return finish_invalid("batch_budget");
  }

  switch (action) {
    case probe::PostSearchAction::kMeasureCandidate:
      g_rtc.post_ms = probe::PostSearchCurrent(g_rtc.post_search);
      return BeginPreparedBatch(probe::ProbeStage::kPostProbeSleep250,
                                BatchSize());
    case probe::PostSearchAction::kSecondBatch:
    case probe::PostSearchAction::kRetryBatch:
      // Same candidate, a fresh and independently identified batch.
      return BeginPreparedBatch(probe::ProbeStage::kPostProbeSleep250,
                                BatchSize());
    case probe::PostSearchAction::kFinishedInvalid:
      return finish_invalid("no_post_delivers");
    case probe::PostSearchAction::kFinishedSelected:
      break;
  }

  g_rtc.post_ms = probe::PostSearchSelected(g_rtc.post_search);
  std::printf("P_POST_SELECTED post=%u\n",
              static_cast<unsigned>(g_rtc.post_ms));
  std::fflush(stdout);
  if (kSmokeMode) {
    // The smoke run only had to prove that the measured path really sleeps.
    probe::ProductProbeSetStage(g_rtc, probe::ProbeStage::kDone);
    return true;
  }
  probe::ProductProbeSetStage(g_rtc, probe::ProbeStage::kHotPrepare);
  return true;
}

// Runs while the Aether app is still alive, so prepared export and Save work.
void FinishWorkInLoop() {
  bool ok = false;
  switch (g_purpose) {
    case FullPurpose::kPreparePostBatch:
      ok =
          g_write_ok && BeginPreparedBatch(
                            probe::ProbeStage::kPostProbeSleep250, BatchSize());
      break;
    case FullPurpose::kPrepareHot:
      ok = g_write_ok && BeginPreparedBatch(probe::ProbeStage::kPpkArm,
                                            probe::kProbeHotCount);
      break;
    case FullPurpose::kQueryPostBatch:
      ok = ApplyPostVerdict();
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
  // The Aether teardown leaves the chip in the same state the ICMP search does,
  // so this transition restarts instead of sleeping.
  RestartToNextStage();
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

// A deep-sleep timer wake continues the run. So does the software restart the
// stage handovers issue, because it preserves the same RTC state.
bool IsColdBoot() {
  auto const reset = static_cast<esp_reset_reason_t>(g_early.reset_reason);
  if (reset == ESP_RST_SW) {
    return false;
  }
  if (reset != ESP_RST_DEEPSLEEP) {
    return true;
  }
  return static_cast<esp_sleep_wakeup_cause_t>(g_early.wakeup_cause) !=
         ESP_SLEEP_WAKEUP_TIMER;
}

void PrepareOnBoot() {
  g_early = GetExperimentEarlyEntrySnapshot();
  ComputeWakeMetrics();
  bool const cold = IsColdBoot() || g_rtc.stage >= probe::kProbeStageCount;

  BootMark mark{};
  mark.stage = g_rtc.stage;
  mark.reset_reason = g_early.reset_reason;
  mark.wakeup_cause = g_early.wakeup_cause;
  mark.cold = cold ? 1 : 0;
  if (cold) {
    g_rtc_boot_mark_count = 0;
  }
  if (g_rtc_boot_mark_count < kBootMarkCount) {
    g_rtc_boot_marks[g_rtc_boot_mark_count] = mark;
  }
  if (g_rtc_boot_mark_count < 0xff) {
    ++g_rtc_boot_mark_count;
  }

  if (cold) {
    g_rtc_wifi_cache = prepared_send::PreparedWifiRtcCache{};
    g_icmp_cache = IcmpCache{};
    g_rtc_batch_attempts = 0;
    g_rtc_total_batches = 0;
    g_rtc_sleep_arm_us = 0;
    g_rtc_sleep_reject = 0;
    g_rtc_sleep_reject_err = 0;
    g_rtc_timer_wakes = 0;
    g_rtc_bad_wakes = 0;
    probe::ProductProbeColdBootReset(g_rtc, esp_random());
  } else if (g_woke_from_timer_deep_sleep && g_rtc_timer_wakes < 0xffff) {
    ++g_rtc_timer_wakes;
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
  if (stage == probe::ProbeStage::kFullPreparePostBatch) {
    SayStage("begin");
    StartFullStage(FullPurpose::kPreparePostBatch);
    return;
  }
  if (stage == probe::ProbeStage::kPostQuery) {
    SayStage("begin");
    StartFullStage(FullPurpose::kQueryPostBatch);
    return;
  }
  if (stage == probe::ProbeStage::kHotPrepare) {
    SayStage("begin");
    StartFullStage(FullPurpose::kPrepareHot);
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
  // PPK_ARM and the measured batch stages run synchronously from loop().
}

void loop() {
  using namespace temp_sensor;  // NOLINT
  if (g_idle) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    return;
  }

  auto const stage = Stage();

  // A FULL stage advances the stage counter before it tears the app down, so
  // the prepared stages must only start once the app is gone. Otherwise the
  // prepared send races the still-running Aether Wi-Fi.
  if (!g_app) {
    if (stage == probe::ProbeStage::kPpkArm) {
      RunPpkArmStage();
    }
    if (StageIsMeasured(stage)) {
      RunMeasuredBatchStage();
    }
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
      // The wait must stay bounded. With nothing scheduled it blocks until the
      // next task, and a query whose answer never arrives would otherwise hold
      // the stage open past its timeout.
      auto const cap = ae::Now() + std::chrono::milliseconds{200};
      g_app->WaitUntil(next_time < cap ? next_time : cap);
    }
    return;
  }

  EndFullStage();
}

#else

void setup() {}
void loop() {}

#endif
