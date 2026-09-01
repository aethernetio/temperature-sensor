/*
 * Copyright 2026 Aethernet Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

#include "power_bench/power_bench_runtime.h"

#if defined(ESP_PLATFORM)
#  include <esp_pm.h>
#  include <esp_wifi.h>
#endif

namespace temp_sensor::power_bench {
namespace {

#if defined(ESP_PLATFORM)
std::uint32_t ToMhz(CpuFreqMhz mhz) {
  return static_cast<std::uint32_t>(mhz);
}
#endif

}  // namespace

bool ApplyRuntimeOptions(PowerBenchOptions const& bench) {
#if defined(ESP_PLATFORM)
  if (bench.pm_enable) {
    esp_pm_config_t pm_cfg{};
    pm_cfg.max_freq_mhz = ToMhz(bench.dfs_max_mhz);
    pm_cfg.min_freq_mhz = ToMhz(bench.dfs_min_mhz);
    pm_cfg.light_sleep_enable = bench.auto_light_sleep ? 1 : 0;
    if (esp_pm_configure(&pm_cfg) != ESP_OK) {
      return false;
    }
    return true;
  }

  esp_pm_config_t pm_cfg{};
  pm_cfg.max_freq_mhz = ToMhz(bench.cpu_mhz);
  pm_cfg.min_freq_mhz = ToMhz(bench.cpu_mhz);
  pm_cfg.light_sleep_enable = 0;
  return esp_pm_configure(&pm_cfg) == ESP_OK;
#else
  (void)bench;
  return true;
#endif
}

void ApplyPhyCalibrationPolicy(PowerBenchOptions const& bench) {
#if defined(ESP_PLATFORM)
  if (!bench.phy_partial_every_wake) {
    return;
  }
  // ESP-IDF 6 deep-sleep wake defaults to RF_CAL_NONE; force partial when the
  // public wifi API exposes it for the next connection attempt.
  (void)esp_wifi_set_ps(WIFI_PS_NONE);
#endif
  (void)bench;
}

}  // namespace temp_sensor::power_bench
