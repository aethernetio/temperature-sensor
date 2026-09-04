/*
 * Copyright 2026 Aethernet Inc.
 *
 * First-instruction app_main hook for deep-sleep timing experiments.
 * No heap, no logging, no formatting.
 */

#ifndef TEMP_SENSOR_EXPERIMENT_EARLY_ENTRY_H_
#define TEMP_SENSOR_EXPERIMENT_EARLY_ENTRY_H_

#include <cstdint>

struct ExperimentEarlyEntrySnapshot {
  std::int64_t app_entry_esp_timer_us{0};
  std::uint64_t app_entry_rtc_us{0};
  std::uint8_t reset_reason{0};
  std::uint8_t wakeup_cause{0};
  std::uint8_t valid{0};
};

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
     defined(AETHER_DIAG_DEEP_SLEEP_ONLY_10MIN))
extern "C" void ExperimentEarlyAppEntry();
ExperimentEarlyEntrySnapshot const& GetExperimentEarlyEntrySnapshot();
#else
inline void ExperimentEarlyAppEntry() {}
inline ExperimentEarlyEntrySnapshot const& GetExperimentEarlyEntrySnapshot() {
  static ExperimentEarlyEntrySnapshot empty{};
  return empty;
}
#endif
#endif  // TEMP_SENSOR_EXPERIMENT_EARLY_ENTRY_H_
