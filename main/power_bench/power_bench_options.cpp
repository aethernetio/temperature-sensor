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

#include "power_bench/power_bench_options.h"

#include <cstring>

namespace temp_sensor::power_bench {
namespace {

PowerBenchOptions BaselineA0(std::uint16_t id) {
  PowerBenchOptions o{};
  o.variant_id = id;
  o.teardown = TeardownPolicy::kFull;
  o.pmf = PmfPolicy::kOptional;
  o.disconnected_pm = true;
  o.connected_ps = ConnectedPsMode::kNone;
  o.listen_interval = 1;
  o.phase_ps = PhasePsMode::kM0;
  o.encode_during_association = false;
  o.skip_validate_deep_sleep = false;
  o.phy_partial_every_wake = false;
  o.cpu_mhz = CpuFreqMhz::k160;
  o.pm_enable = false;
  o.auto_light_sleep = false;
  o.dfs_max_mhz = CpuFreqMhz::k160;
  o.dfs_min_mhz = CpuFreqMhz::k40;
  return o;
}

bool IsAethernetio(char const* ap) {
  return ap != nullptr && std::strcmp(ap, "aethernetio") == 0;
}

}  // namespace

prepared_send::FastPathConfig MakeApFastPath(char const* ap_ssid,
                                             PowerBenchOptions const& bench) {
  auto cfg = prepared_send::FastPathConfigForProbeProfile(
      ae::WifiProbeProfile::kP4ChannelIpArp,
      IsAethernetio(ap_ssid) ? 0 : 25, 0);
  cfg.auth = prepared_send::FastAuthMode::kWpa2;
  cfg.post_mode = prepared_send::FastPostMode::kTxDoneCb;
  cfg.tx_done_wait =
      prepared_send::FastTxDoneWaitMode::kFirstSuccessNoObserve;
  cfg.ps_max_modem = false;
  prepared_send::ApplyPowerBenchToFastPath(
      &cfg, static_cast<std::uint8_t>(bench.teardown),
      bench.pmf == PmfPolicy::kOff,
      static_cast<std::uint8_t>(bench.connected_ps), bench.listen_interval,
      static_cast<std::uint8_t>(bench.phase_ps),
      bench.encode_during_association);
  return cfg;
}

PowerBenchOptions BuildVariant(std::uint16_t variant_id, char const* ap_ssid) {
  (void)ap_ssid;
  switch (variant_id) {
    case 0:
      return BaselineA0(0);
    case 1:
      return BaselineA0(1);
    case 10:
      {
        auto o = BaselineA0(10);
        o.skip_validate_deep_sleep = true;
        return o;
      }
    case 11:
      {
        auto o = BaselineA0(11);
        o.disconnected_pm = false;
        return o;
      }
    case 12:
      {
        auto o = BaselineA0(12);
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 13:
      {
        auto o = BaselineA0(13);
        o.connected_ps = ConnectedPsMode::kMaxModem;
        o.listen_interval = 1;
        o.phase_ps = PhasePsMode::kM2;
        return o;
      }
    case 14:
      {
        auto o = BaselineA0(14);
        o.connected_ps = ConnectedPsMode::kMaxModem;
        o.listen_interval = 3;
        o.phase_ps = PhasePsMode::kM2;
        return o;
      }
    case 15:
      {
        auto o = BaselineA0(15);
        o.cpu_mhz = CpuFreqMhz::k120;
        return o;
      }
    case 16:
      {
        auto o = BaselineA0(16);
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 17:
      {
        auto o = BaselineA0(17);
        o.cpu_mhz = CpuFreqMhz::k40;
        return o;
      }
    case 18:
      {
        auto o = BaselineA0(18);
        o.pmf = PmfPolicy::kOff;
        return o;
      }
    case 19:
      {
        auto o = BaselineA0(19);
        o.encode_during_association = true;
        return o;
      }
    case 20:
      {
        auto o = BaselineA0(20);
        o.teardown = TeardownPolicy::kStopOnly;
        return o;
      }
    case 21:
      {
        auto o = BaselineA0(21);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        return o;
      }
    case 22:
      {
        auto o = BaselineA0(22);
        o.phy_partial_every_wake = true;
        return o;
      }
    case 100:
      {
        auto o = BaselineA0(100);
        o.cpu_mhz = CpuFreqMhz::k80;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 101:
      {
        auto o = BaselineA0(101);
        o.cpu_mhz = CpuFreqMhz::k80;
        o.connected_ps = ConnectedPsMode::kMaxModem;
        o.listen_interval = 1;
        o.phase_ps = PhasePsMode::kM2;
        return o;
      }
    case 110:
      {
        auto o = BaselineA0(110);
        o.pm_enable = true;
        o.dfs_max_mhz = CpuFreqMhz::k160;
        o.dfs_min_mhz = CpuFreqMhz::k40;
        o.auto_light_sleep = false;
        return o;
      }
    case 111:
      {
        auto o = BaselineA0(111);
        o.pm_enable = true;
        o.auto_light_sleep = true;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 112:
      {
        auto o = BaselineA0(112);
        o.pm_enable = true;
        o.auto_light_sleep = true;
        o.connected_ps = ConnectedPsMode::kMaxModem;
        o.listen_interval = 1;
        o.phase_ps = PhasePsMode::kM2;
        return o;
      }
    case 120:
      {
        auto o = BaselineA0(120);
        o.phase_ps = PhasePsMode::kM3;
        return o;
      }
    case 121:
      {
        auto o = BaselineA0(121);
        o.phase_ps = PhasePsMode::kM4;
        return o;
      }
    case 130:
      {
        auto o = BaselineA0(130);
        o.cpu_mhz = CpuFreqMhz::k80;
        o.encode_during_association = true;
        return o;
      }
    case 131:
      {
        auto o = BaselineA0(131);
        o.cpu_mhz = CpuFreqMhz::k80;
        o.pmf = PmfPolicy::kOff;
        return o;
      }
    case 140:
      {
        auto o = BaselineA0(140);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 150:
      {
        auto o = BaselineA0(150);
        o.phy_partial_every_wake = true;
        return o;
      }
    case 200:
      return BaselineA0(200);
    case 201:
      {
        auto o = BaselineA0(201);
        o.disconnected_pm = false;
        return o;
      }
    case 202:
      {
        auto o = BaselineA0(202);
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 203:
      {
        auto o = BaselineA0(203);
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 204:
      {
        auto o = BaselineA0(204);
        o.pm_enable = true;
        o.auto_light_sleep = true;
        o.dfs_max_mhz = CpuFreqMhz::k160;
        o.dfs_min_mhz = CpuFreqMhz::k40;
        return o;
      }
    case 205:
      {
        auto o = BaselineA0(205);
        o.cpu_mhz = CpuFreqMhz::k80;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        o.encode_during_association = true;
        o.teardown = TeardownPolicy::kStopOnly;
        return o;
      }
    case 206:
      {
        auto o = BaselineA0(206);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        return o;
      }
    case 207:
      {
        auto o = BaselineA0(207);
        o.pmf = PmfPolicy::kOff;
        return o;
      }
    case 208:
      {
        auto o = BaselineA0(208);
        o.phy_partial_every_wake = true;
        return o;
      }
    // Confirmation-study combinations (DirectDeepSleep = IO_TEARDOWN semantics).
    case 300:
      {
        auto o = BaselineA0(300);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.skip_validate_deep_sleep = true;
        return o;
      }
    case 301:
      {
        auto o = BaselineA0(301);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 302:
      {
        auto o = BaselineA0(302);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 303:
      {
        auto o = BaselineA0(303);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.disconnected_pm = false;
        return o;
      }
    case 310:
      {
        auto o = BaselineA0(310);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.skip_validate_deep_sleep = true;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 311:
      {
        auto o = BaselineA0(311);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 312:
      {
        auto o = BaselineA0(312);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.skip_validate_deep_sleep = true;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 313:
      {
        auto o = BaselineA0(313);
        o.teardown = TeardownPolicy::kDirectDeepSleep;
        o.skip_validate_deep_sleep = true;
        o.disconnected_pm = false;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 314:
      {
        // Same as 313 without DirectDeepSleep — full teardown + all other wins.
        auto o = BaselineA0(314);
        o.skip_validate_deep_sleep = true;
        o.disconnected_pm = false;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 315:
      {
        auto o = BaselineA0(315);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        return o;
      }
    case 316:
      {
        auto o = BaselineA0(316);
        o.skip_validate_deep_sleep = true;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 317:
      {
        auto o = BaselineA0(317);
        o.skip_validate_deep_sleep = true;
        o.disconnected_pm = false;
        return o;
      }
    case 318:
      {
        auto o = BaselineA0(318);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.connected_ps = ConnectedPsMode::kMinModem;
        o.phase_ps = PhasePsMode::kM1;
        return o;
      }
    case 319:
      {
        auto o = BaselineA0(319);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.disconnected_pm = false;
        return o;
      }
    // Best-config STOP-only matrix (SKIP + CPU80 + teardown variants).
    // P0 control: confirmed portable winner with FULL teardown.
    case 400:
      {
        auto o = BaselineA0(400);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kFull;
        return o;
      }
    // P1: STOP_FULL_SAFE
    case 401:
      {
        auto o = BaselineA0(401);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopOnly;
        return o;
      }
    // P2: STOP_MINIMAL
    case 402:
      {
        auto o = BaselineA0(402);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    // P3: STOP_DISCONNECT
    case 403:
      {
        auto o = BaselineA0(403);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopDisconnect;
        return o;
      }
    // Ablation / CPU retune around STOP (defaults to StopMinimal; runner picks).
    case 500:  // A: SKIP + CPU80 + STOP_MINIMAL
      {
        auto o = BaselineA0(500);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    case 501:  // B: SKIP + CPU160 + STOP_MINIMAL
      {
        auto o = BaselineA0(501);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k160;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    case 502:  // C: NO_SKIP + CPU80 + STOP_MINIMAL
      {
        auto o = BaselineA0(502);
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    case 503:  // D: SKIP + CPU80 + FULL
      {
        auto o = BaselineA0(503);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kFull;
        return o;
      }
    case 504:  // E: SKIP + CPU160 + FULL
      {
        auto o = BaselineA0(504);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k160;
        o.teardown = TeardownPolicy::kFull;
        return o;
      }
    case 505:  // F: NO_SKIP + CPU80 + FULL
      {
        auto o = BaselineA0(505);
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kFull;
        return o;
      }
    case 510:  // SKIP + STOP_MINIMAL + CPU160
      {
        auto o = BaselineA0(510);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k160;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    case 511:  // SKIP + STOP_MINIMAL + CPU120
      {
        auto o = BaselineA0(511);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k120;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    case 512:  // SKIP + STOP_MINIMAL + CPU80
      {
        auto o = BaselineA0(512);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    case 513:  // SKIP + STOP_MINIMAL + CPU40
      {
        auto o = BaselineA0(513);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k40;
        o.teardown = TeardownPolicy::kStopMinimal;
        return o;
      }
    case 520:  // BEST STOP + encode_during_association
      {
        auto o = BaselineA0(520);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopMinimal;
        o.encode_during_association = true;
        return o;
      }
    // Same as 401/402/403 but with STOP_FULL_SAFE for ablation if minimal fails.
    case 530:
      {
        auto o = BaselineA0(530);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopOnly;
        return o;
      }
    case 531:
      {
        auto o = BaselineA0(531);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k160;
        o.teardown = TeardownPolicy::kStopOnly;
        return o;
      }
    case 532:
      {
        auto o = BaselineA0(532);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k120;
        o.teardown = TeardownPolicy::kStopOnly;
        return o;
      }
    case 533:
      {
        auto o = BaselineA0(533);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k40;
        o.teardown = TeardownPolicy::kStopOnly;
        return o;
      }
    case 540:  // BEST STOP_FULL_SAFE + encode
      {
        auto o = BaselineA0(540);
        o.skip_validate_deep_sleep = true;
        o.cpu_mhz = CpuFreqMhz::k80;
        o.teardown = TeardownPolicy::kStopOnly;
        o.encode_during_association = true;
        return o;
      }
    default:
      return BaselineA0(variant_id);
  }
}

}  // namespace temp_sensor::power_bench
