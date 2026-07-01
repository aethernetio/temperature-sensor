/*
 * Copyright 2026 Aethernet Inc.
 *
 * Experimental ESP32 prepared-send integration.
 *
 * PreparedSendMessageBlock lives as a plain object in RTC RAM.
 * Presence is tracked only by rtc_prepared_block_valid.
 */

#include "prepared_send/prepared_send.h"

#include <cstring>
#include <iostream>
#include <sstream>

#include "aether/all.h"
#include "aether/mstream.h"
#include "aether/mstream_buffers.h"
#include "aether/prepared_packet/packet_encoder.h"
#include "aether/prepared_packet/prepare_send_message.h"

#if defined(ESP_PLATFORM)
#  include <esp_err.h>
#  include <esp_event.h>
#  include <esp_log.h>
#  include <esp_netif.h>
#  include <esp_wifi.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/event_groups.h>
#  include <lwip/inet.h>
#  include <lwip/sockets.h>
#  include <nvs_flash.h>
#endif

namespace temp_sensor::prepared_send {
namespace {

static constexpr char const* kTag = "prepared-send";

#ifndef AETHER_PREPARED_NONCE_RESERVE
#  define AETHER_PREPARED_NONCE_RESERVE 32
#endif

#ifndef AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS
#  define AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS 15000
#endif

#if defined(ESP_PLATFORM)
RTC_NOINIT_ATTR bool rtc_prepared_block_valid;
RTC_NOINIT_ATTR ae::prepared_packet::PreparedSendMessageBlock rtc_prepared_block;
#else
bool rtc_prepared_block_valid = false;
ae::prepared_packet::PreparedSendMessageBlock rtc_prepared_block{};
#endif

#if defined(ESP_PLATFORM)

static EventGroupHandle_t g_wifi_event_group = nullptr;
static constexpr EventBits_t kWifiConnectedBit = BIT0;
static constexpr EventBits_t kWifiFailBit = BIT1;

void WifiEventHandler(void*, esp_event_base_t event_base,
                      std::int32_t event_id, void*) {
  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
    esp_wifi_connect();
  } else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_DISCONNECTED) {
    xEventGroupSetBits(g_wifi_event_group, kWifiFailBit);
  } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
    xEventGroupSetBits(g_wifi_event_group, kWifiConnectedBit);
  }
}

bool EnsureWifiConnectedForHotPath() {
#ifndef WIFI_SSID
  ESP_LOGE(kTag, "WIFI_SSID is not defined");
  return false;
#endif
#ifndef WIFI_PASSWORD
  ESP_LOGE(kTag, "WIFI_PASSWORD is not defined");
  return false;
#endif

  auto err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
      err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    err = nvs_flash_init();
  }
  if (err != ESP_OK && err != ESP_ERR_NVS_NO_FREE_PAGES) {
    ESP_LOGE(kTag, "nvs_flash_init failed: %s", esp_err_to_name(err));
    return false;
  }

  if (esp_netif_init() != ESP_OK) {
    ESP_LOGW(kTag, "esp_netif_init returned non-OK; continuing");
  }
  if (esp_event_loop_create_default() != ESP_OK) {
    ESP_LOGW(kTag, "event loop already exists or failed; continuing");
  }

  g_wifi_event_group = xEventGroupCreate();
  if (g_wifi_event_group == nullptr) {
    ESP_LOGE(kTag, "failed to create Wi-Fi event group");
    return false;
  }

  esp_netif_create_default_wifi_sta();

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  if (esp_wifi_init(&cfg) != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_init failed");
    return false;
  }

  esp_event_handler_instance_t any_id;
  esp_event_handler_instance_t got_ip;
  esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                      &WifiEventHandler, nullptr, &any_id);
  esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                      &WifiEventHandler, nullptr, &got_ip);

  wifi_config_t wifi_config{};
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.ssid), WIFI_SSID,
               sizeof(wifi_config.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.password), WIFI_PASSWORD,
               sizeof(wifi_config.sta.password));

  if (esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK ||
      esp_wifi_set_config(WIFI_IF_STA, &wifi_config) != ESP_OK ||
      esp_wifi_start() != ESP_OK) {
    ESP_LOGE(kTag, "failed to start Wi-Fi STA");
    return false;
  }

  EventBits_t bits = xEventGroupWaitBits(
      g_wifi_event_group, kWifiConnectedBit | kWifiFailBit, pdFALSE, pdFALSE,
      pdMS_TO_TICKS(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS));

  if ((bits & kWifiConnectedBit) == 0) {
    ESP_LOGE(kTag, "Wi-Fi hot path connect timeout/fail");
    esp_wifi_stop();
    return false;
  }

  return true;
}

std::string PreparedEndpointText(
    ae::prepared_packet::PreparedEndpoint const& endpoint) {
  char buf[INET6_ADDRSTRLEN]{};

  if (endpoint.version == ae::prepared_packet::PreparedIpVersion::kIpV4) {
    if (inet_ntop(AF_INET, endpoint.ip.data(), buf, sizeof(buf)) == nullptr) {
      return {};
    }
  } else {
    if (inet_ntop(AF_INET6, endpoint.ip.data(), buf, sizeof(buf)) == nullptr) {
      return {};
    }
  }

  std::ostringstream ss;
  ss << buf << ":" << endpoint.port;
  return ss.str();
}

bool SendUdpDatagram(ae::prepared_packet::PreparedEndpoint const& endpoint,
                     ae::DataBuffer const& packet) {
  if (endpoint.protocol != ae::Protocol::kUdp) {
    ESP_LOGE(kTag, "endpoint protocol is not UDP");
    return false;
  }

  auto endpoint_text = PreparedEndpointText(endpoint);
  if (endpoint_text.empty()) {
    ESP_LOGE(kTag, "invalid prepared IP endpoint");
    return false;
  }

  sockaddr_storage addr{};
  socklen_t addr_len = 0;
  int fd = -1;

  if (endpoint.version == ae::prepared_packet::PreparedIpVersion::kIpV4) {
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(endpoint.port);
    std::memcpy(&a.sin_addr, endpoint.ip.data(), 4);

    fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    std::memcpy(&addr, &a, sizeof(a));
    addr_len = sizeof(a);
  } else {
    sockaddr_in6 a{};
    a.sin6_family = AF_INET6;
    a.sin6_port = htons(endpoint.port);
    std::memcpy(&a.sin6_addr, endpoint.ip.data(), 16);

    fd = socket(AF_INET6, SOCK_DGRAM, IPPROTO_UDP);
    std::memcpy(&addr, &a, sizeof(a));
    addr_len = sizeof(a);
  }

  if (fd < 0) {
    ESP_LOGE(kTag, "socket() failed");
    return false;
  }

  auto sent = sendto(fd, packet.data(), packet.size(), 0,
                     reinterpret_cast<sockaddr*>(&addr), addr_len);
  close(fd);

  if (sent >= 0 && static_cast<std::size_t>(sent) == packet.size()) {
    ESP_LOGI(kTag, "UDP datagram sent %zu bytes to %s", packet.size(),
             endpoint_text.c_str());
    return true;
  }

  ESP_LOGE(kTag, "UDP send failed: sent=%d size=%zu", static_cast<int>(sent),
           packet.size());
  return false;
}

#else

bool EnsureWifiConnectedForHotPath() { return true; }

bool SendUdpDatagram(ae::prepared_packet::PreparedEndpoint const&,
                     ae::DataBuffer const&) {
  return false;
}

#endif

}  // namespace

char const* ToString(HotSendStatus status) {
  switch (status) {
    case HotSendStatus::kSent:
      return "sent";
    case HotSendStatus::kNoPreparedBlock:
      return "no-prepared-block";
    case HotSendStatus::kNonceExhausted:
      return "nonce-exhausted";
    case HotSendStatus::kEncodeFailed:
      return "encode-failed";
    case HotSendStatus::kWifiFailed:
      return "wifi-failed";
    case HotSendStatus::kSendFailed:
      return "send-failed";
    case HotSendStatus::kUnsupported:
      return "unsupported";
  }
  return "unknown";
}

ae::DataBuffer MakeTemperaturePayload(std::int16_t temperature) {
  struct Header {
    std::uint8_t const root_code = 0x3;
    std::uint8_t const size = sizeof(std::uint8_t) + sizeof(std::int16_t);
    std::uint8_t const dev_code = 0x10;
    AE_REFLECT_MEMBERS(root_code, size, dev_code)
  };

  static constexpr auto header = Header{};

  auto message = ae::DataBuffer{};
  message.reserve(sizeof(header) + sizeof(temperature));
  {
    auto writer = ae::VectorWriter<>{message};
    auto stream = ae::omstream{writer};
    stream << header << temperature;
  }
  return message;
}

bool HasPreparedSendBlock() { return rtc_prepared_block_valid; }

void ClearPreparedSendBlock() { rtc_prepared_block_valid = false; }

bool ExportPreparedSendBlock(ae::AetherApp& app, ae::P2pStream& stream,
                             std::size_t reserve_nonce_count) {
  if (stream.stream_info().link_state != ae::LinkState::kLinked) {
    return false;
  }

  auto prepared_block = ae::prepared_packet::PrepareSendMessage(
      stream, static_cast<std::uint32_t>(reserve_nonce_count));

  if (!prepared_block) {
    std::cerr << "[prepared-send] PrepareSendMessage failed\n";
    return false;
  }

  // PrepareSendMessage already reserved/burned nonce range in full Aether state.
  app.aether().Save();

  rtc_prepared_block = *prepared_block;
  rtc_prepared_block_valid = true;

  std::cout << "[prepared-send] exported prepared block, reserved "
            << reserve_nonce_count << " nonces\n";
  return true;
}

HotSendStatus TryHotWakePreparedSend(std::int16_t temperature) {
  if (!rtc_prepared_block_valid) {
    return HotSendStatus::kNoPreparedBlock;
  }

  if (!EnsureWifiConnectedForHotPath()) {
    ClearPreparedSendBlock();
    return HotSendStatus::kWifiFailed;
  }

  auto block = rtc_prepared_block;
  auto payload = MakeTemperaturePayload(temperature);
  ae::DataBuffer packet;

  auto encode_result = ae::prepared_packet::EncodePacket(block, payload, packet);

  if (!encode_result) {
    ClearPreparedSendBlock();
    if (encode_result.error ==
        ae::prepared_packet::EncodePacketError::kNonceExhausted) {
      return HotSendStatus::kNonceExhausted;
    }
    return HotSendStatus::kEncodeFailed;
  }

  rtc_prepared_block = block;

  if (!SendUdpDatagram(block.endpoint, packet)) {
    ClearPreparedSendBlock();
    return HotSendStatus::kSendFailed;
  }

  std::cout << "[prepared-send] hot path sent temperature " << temperature
            << "\n";
  return HotSendStatus::kSent;
}

}  // namespace temp_sensor::prepared_send
