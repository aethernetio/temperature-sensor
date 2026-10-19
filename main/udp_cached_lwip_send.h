/*
 * Cached Wi-Fi + raw lwIP UDP send (extracted from udp_rtc_index_loop.cpp @ 8f7b8d5).
 * Single integration boundary for Æther HOT and LAN UDP diagnostics.
 */
#ifndef TEMP_SENSOR_UDP_CACHED_LWIP_SEND_H_
#define TEMP_SENSOR_UDP_CACHED_LWIP_SEND_H_

#include <cstddef>
#include <cstdint>

#include "prepared_send/prepared_send.h"

#if defined(ESP_PLATFORM)

namespace temp_sensor::udp_cached_lwip {

struct SendRequest {
  std::uint32_t dest_ipv4{0};  // host byte order (esp_netif ip_info.addr)
  std::uint16_t dest_port{0};
  std::uint8_t const* payload{nullptr};
  std::size_t payload_len{0};
  int post_send_hold_ms{200};  // B default 200; Æther simple uses 1000
  bool use_peer_static_arp{false};  // cloud: gateway ARP only
};

struct SendResult {
  bool got_ip{false};
  bool udp_send_ok{false};
  int lwip_err{0};
};

// One wake cycle: init Wi-Fi → GOT_IP → settle → UDP → hold → teardown.
// Does not deep-sleep or touch GPIO rails.
SendResult SendOnce(char const* ssid, char const* password,
                    prepared_send::PreparedWifiRtcCache const& cache,
                    SendRequest const& req);

}  // namespace temp_sensor::udp_cached_lwip

#endif  // ESP_PLATFORM

#endif  // TEMP_SENSOR_UDP_CACHED_LWIP_SEND_H_
