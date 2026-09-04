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
     defined(AE_EXP_FULL_1MIN_10))

#  include <esp_sleep.h>
#  include <esp_system.h>
#  include <esp_timer.h>

extern "C" std::uint64_t esp_rtc_get_time_us(void);

namespace {
ExperimentEarlyEntrySnapshot g_early{};
}

extern "C" void ExperimentEarlyAppEntry() {
  g_early.app_entry_esp_timer_us = esp_timer_get_time();
  g_early.app_entry_rtc_us = esp_rtc_get_time_us();
  g_early.reset_reason = static_cast<std::uint8_t>(esp_reset_reason());
  g_early.wakeup_cause =
      static_cast<std::uint8_t>(esp_sleep_get_wakeup_cause());
  g_early.valid = 1;
}

ExperimentEarlyEntrySnapshot const& GetExperimentEarlyEntrySnapshot() {
  return g_early;
}

#endif
