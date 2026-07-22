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
#include <chrono>
#include <cstring>
#include <iostream>
#include <limits>
#include <optional>
#include <thread>

#include "aether/all.h"
#include "aether/mstream.h"
#include "aether/mstream_buffers.h"
#include "aether/prepared_packet/packet_encoder.h"

#if defined(ESP_PLATFORM)
#  include <esp_err.h>
#  include <esp_event.h>
#  include <esp_log.h>
#  include <esp_netif.h>
#  include <esp_mac.h>
#  include <esp_wifi.h>
#  include <esp_wifi_default.h>
#  include <esp_private/wifi.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/event_groups.h>
#  include <lwip/inet.h>
#  include <lwip/sockets.h>
#  include <nvs_flash.h>
#endif

namespace temp_sensor::prepared_send {
namespace {
static constexpr char const* kTag = "prepared-send";
static RTC_DATA_ATTR esp_netif_ip_info_t rtc_ip_info = {};
static RTC_DATA_ATTR WiFiBaseStation base_station{};
static RTC_DATA_ATTR bool adress_is_valid{false};
static RTC_DATA_ATTR bool bs_is_valid{false};

#ifndef AETHER_PREPARED_NONCE_RESERVE
#  define AETHER_PREPARED_NONCE_RESERVE 30
#endif

#ifndef AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS
#  define AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS 15000
#endif

#ifndef AETHER_PREPARED_HOT_WIFI_MAX_RETRY
#  define AETHER_PREPARED_HOT_WIFI_MAX_RETRY 10
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

bool FillUdpDestination(ae::prepared_packet::PreparedEndpoint const& endpoint,
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
static esp_netif_t* g_wifi_netif = nullptr;
static esp_event_handler_instance_t g_wifi_any_id_handler = nullptr;
static esp_event_handler_instance_t g_wifi_got_ip_handler = nullptr;
static bool g_wifi_initialized = false;
static bool g_wifi_started = false;
static bool g_default_event_loop_created = false;
static int g_wifi_retry_count = 0;
static constexpr EventBits_t kWifiConnectedBit = BIT0;
static constexpr EventBits_t kWifiFailBit = BIT1;

void WifiEventHandler(void*, esp_event_base_t event_base,
                      std::int32_t event_id, void* event_data) {
  if (g_wifi_event_group == nullptr) {
    return;
  }

  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
    g_wifi_retry_count = 0;
    auto err = esp_wifi_connect();
    if (err != ESP_OK) {
      ESP_LOGE(kTag, "Wi-Fi hot path connect start failed: %s",
               esp_err_to_name(err));
      xEventGroupSetBits(g_wifi_event_group, kWifiFailBit);
    }
  } else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_DISCONNECTED) {
    auto const* event =
        static_cast<wifi_event_sta_disconnected_t const*>(event_data);
    auto const reason = event != nullptr ? static_cast<int>(event->reason) : -1;

    if (g_wifi_retry_count < AETHER_PREPARED_HOT_WIFI_MAX_RETRY) {
      ++g_wifi_retry_count;
      ESP_LOGW(kTag,
               "Wi-Fi hot path disconnected reason=%d; retry %d/%d",
               reason, g_wifi_retry_count,
               static_cast<int>(AETHER_PREPARED_HOT_WIFI_MAX_RETRY));

      auto err = esp_wifi_connect();
      if (err != ESP_OK) {
        ESP_LOGE(kTag, "Wi-Fi hot path reconnect failed: %s",
                 esp_err_to_name(err));
        xEventGroupSetBits(g_wifi_event_group, kWifiFailBit);
      }
    } else {
      ESP_LOGE(kTag, "Wi-Fi hot path retry limit reached; reason=%d", reason);
      xEventGroupSetBits(g_wifi_event_group, kWifiFailBit);
    }
  } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
    ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        if (!adress_is_valid) {
            rtc_ip_info.ip = event->ip_info.ip;
            rtc_ip_info.netmask = event->ip_info.netmask;
            rtc_ip_info.gw = event->ip_info.gw;
           
            wifi_ap_record_t ap_info;
            if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
                base_station.target_channel = ap_info.primary;
                memcpy(base_station.target_bssid, ap_info.bssid, sizeof(base_station.target_bssid));
                 ESP_LOGD(kTag,
                          "Storing to cache BSSID:" MACSTR " CHN:%u",
                          MAC2STR(base_station.target_bssid),
                          static_cast<unsigned>(base_station.target_channel));
                bs_is_valid = true;
            }
            adress_is_valid = true;
        }
    ESP_LOGI(kTag, "Wi-Fi hot path connected after %d retries",
             g_wifi_retry_count);
    xEventGroupSetBits(g_wifi_event_group, kWifiConnectedBit);
  }
}

void CleanupHotPathWifi() {
  if (g_wifi_any_id_handler != nullptr) {
    auto err = esp_event_handler_instance_unregister(
        WIFI_EVENT, ESP_EVENT_ANY_ID, g_wifi_any_id_handler);
    if (err != ESP_OK) {
      ESP_LOGW(kTag, "failed to unregister WIFI handler: %s",
               esp_err_to_name(err));
    }
    g_wifi_any_id_handler = nullptr;
  }

  if (g_wifi_got_ip_handler != nullptr) {
    auto err = esp_event_handler_instance_unregister(
        IP_EVENT, IP_EVENT_STA_GOT_IP, g_wifi_got_ip_handler);
    if (err != ESP_OK) {
      ESP_LOGW(kTag, "failed to unregister IP handler: %s",
               esp_err_to_name(err));
    }
    g_wifi_got_ip_handler = nullptr;
  }

  if (g_wifi_started) {
    auto err = esp_wifi_stop();
    if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_STARTED &&
        err != ESP_ERR_WIFI_NOT_INIT) {
      ESP_LOGW(kTag, "esp_wifi_stop failed during cleanup: %s",
               esp_err_to_name(err));
    }
    g_wifi_started = false;
  }

  if (g_wifi_initialized) {
    auto err = esp_wifi_deinit();
    if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_INIT) {
      ESP_LOGW(kTag, "esp_wifi_deinit failed during cleanup: %s",
               esp_err_to_name(err));
    }
    g_wifi_initialized = false;
  }

  if (g_wifi_netif != nullptr) {
    esp_netif_destroy_default_wifi(g_wifi_netif);
    g_wifi_netif = nullptr;
  }

  if (g_wifi_event_group != nullptr) {
    vEventGroupDelete(g_wifi_event_group);
    g_wifi_event_group = nullptr;
  }

  g_wifi_retry_count = 0;

  if (g_default_event_loop_created) {
    auto err = esp_event_loop_delete_default();
    if (err != ESP_OK) {
      ESP_LOGW(kTag, "esp_event_loop_delete_default failed: %s",
               esp_err_to_name(err));
    }
    g_default_event_loop_created = false;
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

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  wifi_config_t wifi_config{};

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

  CleanupHotPathWifi();

  if (esp_netif_init() != ESP_OK) {
    ESP_LOGW(kTag, "esp_netif_init returned non-OK; continuing");
  }
  err = esp_event_loop_create_default();
  if (err == ESP_OK) {
    g_default_event_loop_created = true;
  } else if (err == ESP_ERR_INVALID_STATE) {
    ESP_LOGW(kTag, "event loop already exists; continuing");
  } else {
    ESP_LOGE(kTag, "esp_event_loop_create_default failed: %s",
             esp_err_to_name(err));
    return false;
  }

  g_wifi_event_group = xEventGroupCreate();
  if (g_wifi_event_group == nullptr) {
    ESP_LOGE(kTag, "failed to create Wi-Fi event group");
    CleanupHotPathWifi();
    return false;
  }

  g_wifi_netif = esp_netif_create_default_wifi_sta();
  if (g_wifi_netif == nullptr) {
    ESP_LOGE(kTag, "failed to create default Wi-Fi STA netif");
    CleanupHotPathWifi();
    return false;
  }

  if (adress_is_valid) {
    std::cout <<  "Restoring netif config\n";
    esp_netif_dhcpc_stop(g_wifi_netif);
    esp_netif_ip_info_t ip_info = {
      .ip = {.addr = rtc_ip_info.ip.addr},
      .netmask = {.addr = rtc_ip_info.netmask.addr},
      .gw = {.addr = rtc_ip_info.gw.addr}
    };
    esp_netif_set_ip_info(g_wifi_netif, &ip_info);
  } else {
    std::cout <<  "Restoring netif config filed\n";
  }

  // We disable aggregation so that the packages go out one by one and quickly
  cfg.ampdu_rx_enable = 0;
  cfg.ampdu_tx_enable = 0;

  err = esp_wifi_init(&cfg);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_init failed: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }
  g_wifi_initialized = true;

  err = esp_event_handler_instance_register(
      WIFI_EVENT, ESP_EVENT_ANY_ID, &WifiEventHandler, nullptr,
      &g_wifi_any_id_handler);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "failed to register WIFI handler: %s",
             esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }

  err = esp_event_handler_instance_register(
      IP_EVENT, IP_EVENT_STA_GOT_IP, &WifiEventHandler, nullptr,
      &g_wifi_got_ip_handler);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "failed to register IP handler: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }

  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.ssid), WIFI_SSID,
               sizeof(wifi_config.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.password), WIFI_PASSWORD,
               sizeof(wifi_config.sta.password));

  if (bs_is_valid) {
    ESP_LOGD(kTag,
             "Restoring cached BSSID:" MACSTR " CHN:%u",
             MAC2STR(base_station.target_bssid),
             static_cast<unsigned>(base_station.target_channel));
    wifi_config.sta.scan_method = WIFI_FAST_SCAN;
    wifi_config.sta.bssid_set = true;
    wifi_config.sta.channel = base_station.target_channel;
    std::memcpy(wifi_config.sta.bssid, base_station.target_bssid,
                sizeof(base_station.target_bssid));
  }

  err = esp_wifi_set_mode(WIFI_MODE_STA);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_set_mode failed: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }

  err = esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_set_config failed: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }

  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
  
  err = esp_wifi_start();
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_start failed: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }
  g_wifi_started = true;

  EventBits_t bits = xEventGroupWaitBits(
      g_wifi_event_group, kWifiConnectedBit | kWifiFailBit, pdFALSE, pdFALSE,
      pdMS_TO_TICKS(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS));

  esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true, (wifi_phy_rate_t)0x0);
  //esp_wifi_internal_set_retry_counter(3, 3); 

  if ((bits & kWifiConnectedBit) == 0) {
    ESP_LOGE(kTag, "Wi-Fi hot path connect timeout/fail");
    CleanupHotPathWifi();
    return false;
  }

  return true;
}

#else

bool EnsureWifiConnectedForHotPath() {
  return true;
}

void CleanupHotPathWifi() {}

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

ae::DataBuffer MakeTemperaturePayload(std::string temperature) {
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

HotSendStatus TryHotWakePreparedSend(std::string temperature) {
  ae::prepared_packet::PreparedSendMessageBlock block;
  //sleep(10);
  if (!DeserializeFromRetained(block)) {
    return HotSendStatus::kNoPreparedBlock;
  }

  if (!EnsureWifiConnectedForHotPath()) {
    return HotSendStatus::kWifiFailed;
  }

  auto fail_after_wifi = [](HotSendStatus status) {
    CleanupHotPathWifi();
    return status;
  };

  auto payload = MakeTemperaturePayload(temperature);

  ae::DataBuffer packet;
  auto encode_result = ae::prepared_packet::EncodePacket(block, payload, packet);

  if (!encode_result) {
    ClearPreparedSendBlock();
    return fail_after_wifi(HotSendStatus::kEncodeFailed);
  }

  // Persist immediately after EncodePacket, before UDP send/sleep, because
  // EncodePacket consumes nonce state.
  if (!SerializeToRetained(block)) {
    return fail_after_wifi(HotSendStatus::kPersistFailed);
  }

#if defined(ESP_PLATFORM)
  auto const& endpoint = block.endpoint;

  sockaddr_storage dest_storage{};
  socklen_t dest_len = 0;
  if (!FillUdpDestination(endpoint, reinterpret_cast<sockaddr*>(&dest_storage),
                          &dest_len)) {
    std::cerr << "[prepared-send] invalid endpoint address\n";
    return fail_after_wifi(HotSendStatus::kSendFailed);
  }

  int sock = socket(
      endpoint.version == ae::prepared_packet::PreparedIpVersion::kIpV6
          ? AF_INET6
          : AF_INET,
      SOCK_DGRAM, IPPROTO_IP);
  if (sock < 0) {
    return fail_after_wifi(HotSendStatus::kSendFailed);
  }

  auto sent = sendto(sock, packet.data(), packet.size(), 0,
                     reinterpret_cast<sockaddr*>(&dest_storage), dest_len);
  close(sock);

  if (sent != static_cast<int>(packet.size())) {
    return fail_after_wifi(HotSendStatus::kSendFailed);
  }
  std::this_thread::sleep_for(std::chrono::milliseconds(450));
  std::cout << "[prepared-send] hot path UDP sent " << sent << " bytes\n";
  return HotSendStatus::kSent;
#else
  return HotSendStatus::kUnsupported;
#endif
}

}  // namespace temp_sensor::prepared_send
