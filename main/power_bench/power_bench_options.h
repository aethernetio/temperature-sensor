/*
 * Copyright 2026 Aethernet Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef TEMP_SENSOR_POWER_BENCH_OPTIONS_H_
#define TEMP_SENSOR_POWER_BENCH_OPTIONS_H_

#include <cstdint>

#include "aether/wifi/wifi_probe_state.h"
#include "prepared_send/prepared_send.h"

namespace temp_sensor::power_bench {

enum class TeardownPolicy : std::uint8_t {
  kFull = 0,
  // STOP_FULL_SAFE: unregister Wi-Fi/IP handlers, esp_wifi_stop, no deinit.
  kStopOnly = 1,
  // DirectDeepSleep: no stop/deinit (AP-dependent; not portable production).
  kDirectDeepSleep = 2,
  // STOP_MINIMAL: esp_wifi_stop only (handlers/netif/event loop kept).
  kStopMinimal = 3,
  // STOP_DISCONNECT: disconnect (+short wait), then stop; no deinit.
  kStopDisconnect = 4,
};

enum class PmfPolicy : std::uint8_t {
  kOptional = 0,
  kOff = 1,
};

enum class ConnectedPsMode : std::uint8_t {
  kNone = 0,
  kMinModem = 1,
  kMaxModem = 2,
};

enum class PhasePsMode : std::uint8_t {
  kM0 = 0,
  kM1 = 1,
  kM2 = 2,
  kM3 = 3,
  kM4 = 4,
};

enum class CpuFreqMhz : std::uint8_t {
  k160 = 160,
  k120 = 120,
  k80 = 80,
  k40 = 40,
};

struct PowerBenchOptions {
  std::uint16_t variant_id{0};
  TeardownPolicy teardown{TeardownPolicy::kFull};
  PmfPolicy pmf{PmfPolicy::kOptional};
  bool disconnected_pm{true};
  ConnectedPsMode connected_ps{ConnectedPsMode::kNone};
  std::uint8_t listen_interval{1};
  PhasePsMode phase_ps{PhasePsMode::kM0};
  bool encode_during_association{false};
  bool skip_validate_deep_sleep{false};
  bool phy_partial_every_wake{false};
  CpuFreqMhz cpu_mhz{CpuFreqMhz::k160};
  bool pm_enable{false};
  bool auto_light_sleep{false};
  CpuFreqMhz dfs_max_mhz{CpuFreqMhz::k160};
  CpuFreqMhz dfs_min_mhz{CpuFreqMhz::k40};
};

// Fixed chirkov/aethernetio profile/PRE/POST for the power study.
prepared_send::FastPathConfig MakeApFastPath(char const* ap_ssid,
                                            PowerBenchOptions const& bench);

PowerBenchOptions BuildVariant(std::uint16_t variant_id, char const* ap_ssid);

}  // namespace temp_sensor::power_bench

#endif  // TEMP_SENSOR_POWER_BENCH_OPTIONS_H_
