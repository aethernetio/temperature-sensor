/*
 * Copyright 2026 Aethernet Inc.
 *
 * Experimental ESP32 prepared-send integration.
 *
 * This file intentionally keeps the hot path isolated from controller.cpp.
 * If hot path fails for any reason, controller.cpp falls back to normal
 * full-Aether boot.
 */

#include "prepared_send/prepared_send.h"

#include <array>
#include <cstring>
#include <iostream>
#include <limits>
#include <optional>

#include "aether/all.h"
#include "aether/mstream.h"
#include "aether/mstream_buffers.h"
#include "aether/prepared_packet/packet_encoder.h"

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
// RTC_NOINIT_ATTR: do not zero on wake from deep sleep.
// It may contain garbage on first boot, so magic/version/checksum validate it.
RTC_NOINIT_ATTR ae::prepared_packet::RetainedPreparedBlock g_retained_prepared_block;

// ESP32-C6 exposes 8 KiB RTC slow memory; the linker asserts the segment fits.
static_assert(sizeof(ae::prepared_packet::RetainedPreparedBlock) <= 8 * 1024,
              "RetainedPreparedBlock must fit in ESP32 RTC slow memory");
#else
ae::prepared_packet::RetainedPreparedBlock g_retained_prepared_block;
#endif

std::uint32_t Checksum(std::uint8_t const* data, std::size_t size) {
  std::uint32_t h = 2166136261u;
  for (std::size_t i = 0; i < size; ++i) {
    h ^= data[i];
    h *= 16777619u;
  }
  return h;
}

bool RetainedLooksValid() {
  auto const& r = g_retained_prepared_block;
  if (r.magic != ae::prepared_packet::kMagic || r.version != ae::prepared_packet::kVersion) {
    return false;
  }
  if (r.size == 0 || r.size > r.bytes.size()) {
    return false;
  }
  return Checksum(r.bytes.data(), r.size) == r.checksum;
}

void SaveRawBlock(ae::DataBuffer const& bytes) {
  auto& r = g_retained_prepared_block;
  std::memset(&r, 0, sizeof(r));

  r.magic = ae::prepared_packet::kMagic;
  r.version = ae::prepared_packet::kVersion;
  r.size = static_cast<std::uint32_t>(bytes.size());
  std::memcpy(r.bytes.data(), bytes.data(), bytes.size());
  r.checksum = Checksum(r.bytes.data(), r.size);
}

bool LoadRawBlock(ae::DataBuffer& bytes) {
  if (!RetainedLooksValid()) {
    return false;
  }

  auto const& r = g_retained_prepared_block;
  bytes.assign(r.bytes.begin(), r.bytes.begin() + r.size);
  return true;
}

template <typename T>
bool SerializeToRetained(T const& value) {
  ae::DataBuffer bytes;
  bytes.reserve(512);

  {
    auto writer = ae::VectorWriter<>{bytes};
    auto os = ae::omstream{writer};
    os << value;
  }

  if (bytes.empty() || bytes.size() > ae::prepared_packet::kMaxPreparedBlockBytes) {
    std::cerr << "[prepared-send] prepared block serialized size invalid: "
              << bytes.size() << "\n";
    return false;
  }

  SaveRawBlock(bytes);
  return true;
}

template <typename T>
bool DeserializeFromRetained(T& value) {
  ae::DataBuffer bytes;
  if (!LoadRawBlock(bytes)) {
    return false;
  }

  auto reader = ae::VectorReader<>{bytes};
  auto is = ae::imstream{reader};
  is >> value;

  return ae::data_was_read(is);
}

#if defined(ESP_PLATFORM)

bool FillUdpDestination(ae::prepared_packet::PreparedUdpEndpoint const& endpoint,
                       sockaddr* dest_addr, socklen_t* dest_len) {
  if (endpoint.version == ae::prepared_packet::PreparedIpVersion::kIpV4) {
    auto* dest = reinterpret_cast<sockaddr_in*>(dest_addr);
    std::memset(dest, 0, sizeof(*dest));
    dest->sin_family = AF_INET;
    dest->sin_port = htons(endpoint.port);
    std::memcpy(&dest->sin_addr.s_addr, endpoint.ip.data(), 4);
    *dest_len = sizeof(*dest);
    return true;
  }

  if (endpoint.version == ae::prepared_packet::PreparedIpVersion::kIpV6) {
    auto* dest = reinterpret_cast<sockaddr_in6*>(dest_addr);
    std::memset(dest, 0, sizeof(*dest));
    dest->sin6_family = AF_INET6;
    dest->sin6_port = htons(endpoint.port);
    std::memcpy(dest->sin6_addr.s6_addr, endpoint.ip.data(), 16);
    *dest_len = sizeof(*dest);
    return true;
  }

  return false;
}

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

  // It is OK if these were already initialized by a previous attempt.
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

#else

bool EnsureWifiConnectedForHotPath() {
  return true;
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
    case HotSendStatus::kPersistFailed:
      return "persist-failed";
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

bool HasPreparedSendBlock() { return RetainedLooksValid(); }

void ClearPreparedSendBlock() {
  std::memset(&g_retained_prepared_block, 0, sizeof(g_retained_prepared_block));
}

std::optional<ae::prepared_packet::PreparedSendMessageBlock> PrepareSendMessage(
    ae::P2pStream& stream, std::size_t reserve_nonce_count) {
  if (reserve_nonce_count == 0 ||
      reserve_nonce_count > std::numeric_limits<std::uint32_t>::max()) {
    return std::nullopt;
  }

  return stream.ExportPreparedSendMessageBlock(
      static_cast<std::uint32_t>(reserve_nonce_count));
}

bool ExportPreparedSendBlock(ae::AetherApp& app,
                             ae::P2pStream& stream,
                             std::size_t reserve_nonce_count) {
  auto prepared_block =
      PrepareSendMessage(stream, reserve_nonce_count);

  if (!prepared_block) {
    std::cerr << "[prepared-send] PrepareSendMessage failed\n";
    return false;
  }

  // Critical ordering:
  // PrepareSendMessage has already reserved/burned a nonce range in the full
  // Aether state. Persist full Aether state only after reserve.
  app.aether().Save();

  if (!SerializeToRetained(*prepared_block)) {
    std::cerr << "[prepared-send] failed to retain prepared block\n";
    return false;
  }

  std::cout << "[prepared-send] exported prepared block, reserved "
            << reserve_nonce_count << " nonces\n";
  return true;
}

HotSendStatus TryHotWakePreparedSend(std::int16_t temperature) {
  ae::prepared_packet::PreparedSendMessageBlock block;
  if (!DeserializeFromRetained(block)) {
    return HotSendStatus::kNoPreparedBlock;
  }

  if (!EnsureWifiConnectedForHotPath()) {
    return HotSendStatus::kWifiFailed;
  }

  auto payload = MakeTemperaturePayload(temperature);

  ae::DataBuffer packet;
  auto encode_result = ae::prepared_packet::EncodePacket(block, payload, packet);

  if (!encode_result) {
    ClearPreparedSendBlock();
    return HotSendStatus::kEncodeFailed;
  }

  // Persist immediately after EncodePacket, before UDP send/sleep, because
  // EncodePacket consumes nonce state.
  if (!SerializeToRetained(block)) {
    return HotSendStatus::kPersistFailed;
  }

#if defined(ESP_PLATFORM)
  auto const& endpoint = block.endpoint;

  sockaddr_storage dest_storage{};
  socklen_t dest_len = 0;
  if (!FillUdpDestination(endpoint, reinterpret_cast<sockaddr*>(&dest_storage),
                          &dest_len)) {
    std::cerr << "[prepared-send] invalid endpoint address\n";
    return HotSendStatus::kSendFailed;
  }

  int sock = socket(
      endpoint.version == ae::prepared_packet::PreparedIpVersion::kIpV6
          ? AF_INET6
          : AF_INET,
      SOCK_DGRAM, IPPROTO_IP);
  if (sock < 0) {
    return HotSendStatus::kSendFailed;
  }

  auto sent = sendto(sock, packet.data(), packet.size(), 0,
                     reinterpret_cast<sockaddr*>(&dest_storage), dest_len);
  close(sock);

  if (sent != static_cast<int>(packet.size())) {
    return HotSendStatus::kSendFailed;
  }

  std::cout << "[prepared-send] hot path UDP sent " << sent << " bytes\n";
  return HotSendStatus::kSent;
#else
  return HotSendStatus::kUnsupported;
#endif
}

}  // namespace temp_sensor::prepared_send
