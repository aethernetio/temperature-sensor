/*
 * Copyright 2026 Aethernet Inc.
 *
 * Early app_main capture for deep-sleep prepared experiments.
 */

#include "experiment_early_entry.h"

#if defined(ESP_PLATFORM) && \
    (defined(AE_EXP_PREPARED_DEEPSLEEP_5X50) || \
     defined(AE_EXP_PREPARED_FINAL_D1_5X50) || \
     defined(AE_EXP_PREPARED_AP_AETHERNETIO_3X10) || \
     defined(AE_EXP_ADAPTIVE_WIFI_PROBE_C) || \
     defined(AE_EXP_PRODUCT_ADAPTIVE_PROBE) || \
     defined(AE_EXP_PREPARED_TX_DONE_DIAG) || \
     defined(AE_EXP_PREPARED_MAC_RETRY_DIAG) || \
     defined(AE_EXP_PREPARED_BOOT_WIFI_OPT) || \
     defined(AE_EXP_PREPARED_BOOT_WIFI_VAL100) || \
     defined(AE_EXP_PREPARED_POWER_FACTOR) || \
     defined(AE_EXP_PREPARED_FINAL_1MIN_100) || \
     defined(AE_EXP_CACHED_FULL_HOT_1MIN) || \
     defined(AE_EXP_FULL_1MIN_10) || \
     defined(AETHER_DIAG_DEEP_SLEEP_ONLY_10MIN) || \
     defined(AETHER_DIAG_UDP_RTC_INDEX) || \
     defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10) || \
     defined(AETHER_DIAG_UDP_VANILLA_100X_20S) || \
     defined(AETHER_DIAG_WIFI_CONNECT_STAGE_100X) || \
     defined(AETHER_DIAG_SLEEP_POWER_BISECT))

#  include <esp_sleep.h>
#  include <esp_system.h>
#  include <esp_timer.h>
#  include <soc/soc_caps.h>

#  if defined(AETHER_DIAG_UDP_RTC_INDEX) || \
      defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
#    include "udp_rtc_index_loop.h"
#  endif
#  if defined(AETHER_DIAG_UDP_VANILLA_100X_20S)
#    include "udp_vanilla_100x_20s.h"
#  endif
#  if defined(AETHER_DIAG_WIFI_CONNECT_STAGE_100X)
#    include "wifi_connect_stage_100x.h"
#  endif
#  if defined(AETHER_DIAG_SLEEP_POWER_BISECT)
#    include "sleep_power_bisect.h"
#  endif

extern "C" std::uint64_t esp_rtc_get_time_us(void);

namespace {
ExperimentEarlyEntrySnapshot g_early{};
}

extern "C" void ExperimentEarlyAppEntry() {
#if defined(AETHER_DIAG_WIFI_CONNECT_STAGE_100X)
  RunWifiConnectStage100x();
#elif defined(AETHER_DIAG_UDP_VANILLA_100X_20S)
  RunUdpVanilla100x20s();
#elif defined(AETHER_DIAG_UDP_RTC_INDEX) || \
    defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
  RunUdpRtcIndexLoop();
#elif defined(AETHER_DIAG_SLEEP_POWER_BISECT)
  RunSleepPowerBisect();
#elif defined(AETHER_DIAG_DEEP_SLEEP_ONLY_10MIN)
  // Isolation: no Wi-Fi / app init. Timer deep sleep 1h; force-off available PD domains.
  esp_sleep_enable_timer_wakeup(1ULL * 60ULL * 60ULL * 1000000ULL);
  // ESP32-C6: RTC FAST/SLOW mem PD enums are not exposed; without RTC_ATTR/ULP they
  // power down automatically. Force remaining domains off for min sleep current.
#  if SOC_PM_SUPPORT_RTC_PERIPH_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_OFF);
#  endif
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL, ESP_PD_OPTION_OFF);
#  if SOC_PM_SUPPORT_XTAL32K_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL32K, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RC32K_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RC32K, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RC_FAST_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RC_FAST, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_CPU_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_CPU, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_VDDSDIO_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_VDDSDIO, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_MODEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_MODEM, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_TOP_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_TOP, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_CNNT_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_CNNT, ESP_PD_OPTION_OFF);
#  endif
  esp_deep_sleep_start();
#else
  g_early.app_entry_esp_timer_us = esp_timer_get_time();
  g_early.app_entry_rtc_us = esp_rtc_get_time_us();
  g_early.reset_reason = static_cast<std::uint8_t>(esp_reset_reason());
  g_early.wakeup_cause =
      static_cast<std::uint8_t>(esp_sleep_get_wakeup_cause());
  g_early.valid = 1;
#endif
}

ExperimentEarlyEntrySnapshot const& GetExperimentEarlyEntrySnapshot() {
  return g_early;
}

#endif
