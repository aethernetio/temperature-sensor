/*
 * Copyright 2026 Aethernet Inc.
 *
 * Compatibility shim for pinned aether-client-cpp builds that predate
 * aether/wifi/wifi_probe_state.h. HOT / prepared bench uses P4 cached Wi-Fi only.
 */
#ifndef AETHER_WIFI_WIFI_PROBE_STATE_H_
#define AETHER_WIFI_WIFI_PROBE_STATE_H_

#include <cstdint>
#include <cstring>

namespace ae {

enum class WifiProbeProfile : std::uint8_t {
  kP0Default = 0,
  kP1CachedIp = 1,
  kP2ChannelOnly = 2,
  kP3ChannelIp = 3,
  kP4ChannelIpArp = 4,
  kCount = 5,
};

enum class WifiProbeRecoveryReason : std::uint8_t {
  kConnectFailed = 0,
  kIpTimeout = 1,
  kSendFailed = 2,
};

struct WifiProbeRtcState {
  static constexpr std::uint32_t kMagic = 0xAE501070u;
  static constexpr std::uint8_t kVersion = 1;

  std::uint32_t magic{0};
  std::uint8_t version{0};
  std::uint8_t selected_profile{0};
  std::uint16_t pre_send_delay_ms{0};
  std::uint16_t post_send_delay_ms{0};
  std::uint8_t channel{0};
  std::uint32_t ip{0};
  std::uint32_t netmask{0};
  std::uint32_t gateway{0};
  std::uint8_t gateway_mac[6]{};
  std::uint8_t bssid[6]{};
  std::uint32_t ssid_hash{0};
  std::uint32_t crc{0};
};

inline bool WifiProbeProfileUsesChannel(WifiProbeProfile profile) {
  switch (profile) {
    case WifiProbeProfile::kP2ChannelOnly:
    case WifiProbeProfile::kP3ChannelIp:
    case WifiProbeProfile::kP4ChannelIpArp:
      return true;
    default:
      return false;
  }
}

inline bool WifiProbeProfileUsesCachedIp(WifiProbeProfile profile) {
  switch (profile) {
    case WifiProbeProfile::kP1CachedIp:
    case WifiProbeProfile::kP3ChannelIp:
    case WifiProbeProfile::kP4ChannelIpArp:
      return true;
    default:
      return false;
  }
}

inline bool WifiProbeProfileUsesArp(WifiProbeProfile profile) {
  return profile == WifiProbeProfile::kP4ChannelIpArp;
}

inline std::uint32_t WifiProbeHashSsid(char const* ssid) {
  std::uint32_t h = 2166136261u;
  if (ssid == nullptr) {
    return h;
  }
  for (char const* p = ssid; *p != '\0'; ++p) {
    h ^= static_cast<std::uint8_t>(*p);
    h *= 16777619u;
  }
  return h;
}

inline void WifiProbeSealCrc(WifiProbeRtcState& state) {
  state.crc = 0;
  auto const* bytes = reinterpret_cast<std::uint8_t const*>(&state);
  std::uint32_t c = 0;
  for (std::size_t i = 0; i < sizeof(state) - sizeof(state.crc); ++i) {
    c = (c >> 1) ^ ((c & 1u) ? 0xEDB88320u : 0u) ^ bytes[i];
  }
  state.crc = c;
}

inline void WifiProbeDegradeSelected(WifiProbeRtcState& state,
                                     WifiProbeRecoveryReason reason) {
  (void)reason;
  if (state.selected_profile > 0) {
    --state.selected_profile;
  }
  WifiProbeSealCrc(state);
}

}  // namespace ae

#endif  // AETHER_WIFI_WIFI_PROBE_STATE_H_
