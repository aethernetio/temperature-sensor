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
#include <limits>
#include <optional>
#include <thread>

#include "aether-miscpp/serialization/binary_archive.h"
#include "aether-miscpp/misc/defer.h"

#include "aether/all.h"
#include "aether/prepared_packet/packet_encoder.h"
#include "aether/prepared_packet/prepared_send_message.h"

#if !defined(ESP_PLATFORM)
#  include <iostream>
#endif

#if defined(ESP_PLATFORM)
#  include <esp_sleep.h>
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
#  include "wifi_lifecycle_out.h"
#endif

namespace temp_sensor::prepared_send {
#if defined(ESP_PLATFORM)
static char g_last_hot_wifi_fail[64] = {};

static void SetHotWifiFail(char const* msg) {
  std::strncpy(g_last_hot_wifi_fail, msg, sizeof(g_last_hot_wifi_fail) - 1);
  g_last_hot_wifi_fail[sizeof(g_last_hot_wifi_fail) - 1] = '\0';
}
#endif

namespace {

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
static constexpr char const* kTag = "prepared-send";

// RTC_NOINIT_ATTR: do not zero on wake from deep sleep.
// It may contain garbage on first boot, so magic validate it.
static RTC_NOINIT_ATTR ae::prepared_packet::PreparedSendMessageBlock
    g_prepared_send_message_block;

static RTC_DATA_ATTR esp_netif_ip_info_t rtc_ip_info = {};
static RTC_DATA_ATTR WiFiBaseStation base_station{};
static RTC_NOINIT_ATTR bool address_is_valid;
static RTC_NOINIT_ATTR bool bs_is_valid;

// ESP32-C6 exposes 8 KiB RTC slow memory; the linker asserts the segment fits.
static_assert(sizeof(ae::prepared_packet::PreparedSendMessageBlock) <= 8 * 1024,
              "PreparedSendMessageBlock must fit in ESP32 RTC slow memory");
#else
ae::prepared_packet::PreparedSendMessageBlock g_prepared_send_message_block;
#endif

#if defined(ESP_PLATFORM)

bool FillUdpDestination(ae::prepared_packet::PreparedEndpoint const& endpoint,
                        sockaddr* dest_addr, socklen_t* dest_len) {
  if (endpoint.address.Index() == ae::AddrVersion::kIpV4) {
    auto* dest = reinterpret_cast<sockaddr_in*>(dest_addr);
    std::memset(dest, 0, sizeof(*dest));
    dest->sin_family = AF_INET;
    dest->sin_port = htons(endpoint.port);
    std::memcpy(&dest->sin_addr.s_addr,
                &endpoint.address.Get<ae::IpV4Addr>().ipv4_value, 4);
    *dest_len = sizeof(*dest);
    return true;
  }

  if (endpoint.address.Index() == ae::AddrVersion::kIpV6) {
    auto* dest = reinterpret_cast<sockaddr_in6*>(dest_addr);
    std::memset(dest, 0, sizeof(*dest));
    dest->sin6_family = AF_INET6;
    dest->sin6_port = htons(endpoint.port);
    std::memcpy(&dest->sin6_addr.s6_addr,
                &endpoint.address.Get<ae::IpV6Addr>().ipv6_value, 16);
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

void WifiEventHandler(void*, esp_event_base_t event_base, std::int32_t event_id,
                      void* event_data) {
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
      ESP_LOGW(kTag, "Wi-Fi hot path disconnected reason=%d; retry %d/%d",
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
    ip_event_got_ip_t* event = (ip_event_got_ip_t*)event_data;
    if (!address_is_valid) {
      rtc_ip_info.ip = event->ip_info.ip;
      rtc_ip_info.netmask = event->ip_info.netmask;
      rtc_ip_info.gw = event->ip_info.gw;

      wifi_ap_record_t ap_info;
      if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
        base_station.target_channel = ap_info.primary;
        memcpy(base_station.target_bssid, ap_info.bssid,
               sizeof(base_station.target_bssid));
        ESP_LOGD(kTag, "Storing to cache BSSID:" MACSTR " CHN:%u",
                 MAC2STR(base_station.target_bssid),
                 static_cast<unsigned>(base_station.target_channel));
        bs_is_valid = true;
      }
      address_is_valid = true;
    }
    ESP_LOGI(kTag, "Wi-Fi hot path connected after %d retries",
             g_wifi_retry_count);
    xEventGroupSetBits(g_wifi_event_group, kWifiConnectedBit);
  }
}

void CleanupHotPathWifi() {
  address_is_valid = false;
  bs_is_valid = false;
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
#  ifndef WIFI_SSID
  SetHotWifiFail("WIFI_SSID undefined");
  ESP_LOGE(kTag, "WIFI_SSID is not defined");
  return false;
#  endif
#  ifndef WIFI_PASSWORD
  SetHotWifiFail("WIFI_PASSWORD undefined");
  ESP_LOGE(kTag, "WIFI_PASSWORD is not defined");
  return false;
#  endif

  SetHotWifiFail("");
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
    SetHotWifiFail("nvs_flash_init");
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
    SetHotWifiFail("event_loop_create");
    ESP_LOGE(kTag, "esp_event_loop_create_default failed: %s",
             esp_err_to_name(err));
    return false;
  }

  g_wifi_event_group = xEventGroupCreate();
  if (g_wifi_event_group == nullptr) {
    SetHotWifiFail("event_group_create");
    ESP_LOGE(kTag, "failed to create Wi-Fi event group");
    CleanupHotPathWifi();
    return false;
  }

  g_wifi_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if (g_wifi_netif == nullptr) {
    g_wifi_netif = esp_netif_create_default_wifi_sta();
  }
  if (g_wifi_netif == nullptr) {
    SetHotWifiFail("create_default_wifi_sta");
    ESP_LOGE(kTag, "failed to create default Wi-Fi STA netif");
    CleanupHotPathWifi();
    return false;
  }

  if (address_is_valid) {
    ESP_LOGI(kTag, "Restoring netif config");
    esp_netif_dhcpc_stop(g_wifi_netif);
    esp_netif_ip_info_t ip_info = {
        .ip = {.addr = rtc_ip_info.ip.addr},
        .netmask = {.addr = rtc_ip_info.netmask.addr},
        .gw = {.addr = rtc_ip_info.gw.addr}};
    esp_netif_set_ip_info(g_wifi_netif, &ip_info);
  } else {
    ESP_LOGI(kTag, "No cached netif config");
  }

  // We disable aggregation so that the packages go out one by one and quickly
  cfg.ampdu_rx_enable = 0;
  cfg.ampdu_tx_enable = 0;

  err = esp_wifi_init(&cfg);
  if (err == ESP_ERR_WIFI_INIT_STATE) {
    g_wifi_initialized = true;
  } else if (err != ESP_OK) {
    SetHotWifiFail("esp_wifi_init");
    ESP_LOGE(kTag, "esp_wifi_init failed: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  } else {
    g_wifi_initialized = true;
  }

  err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                            &WifiEventHandler, nullptr,
                                            &g_wifi_any_id_handler);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "failed to register WIFI handler: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }

  err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                            &WifiEventHandler, nullptr,
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
    ESP_LOGD(kTag, "Restoring cached BSSID:" MACSTR " CHN:%u",
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

  esp_wifi_set_protocol(
      WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

  err = esp_wifi_start();
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_start failed: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }
  g_wifi_started = true;

  err = esp_wifi_set_max_tx_power(80);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_set_max_tx_power failed: %s",
             esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }
  err = esp_wifi_set_ps(WIFI_PS_MAX_MODEM);
  if (err != ESP_OK) {
    ESP_LOGE(kTag, "esp_wifi_set_ps failed: %s", esp_err_to_name(err));
    CleanupHotPathWifi();
    return false;
  }

  EventBits_t bits = xEventGroupWaitBits(
      g_wifi_event_group, kWifiConnectedBit | kWifiFailBit, pdFALSE, pdFALSE,
      pdMS_TO_TICKS(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS));

  esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true, (wifi_phy_rate_t)0x0);
  // esp_wifi_internal_set_retry_counter(3, 3);

  if ((bits & kWifiConnectedBit) == 0) {
    SetHotWifiFail("connect_timeout");
    ESP_LOGE(kTag, "Wi-Fi hot path connect timeout/fail");
    CleanupHotPathWifi();
    return false;
  }

  return true;
}

#else

bool EnsureWifiConnectedForHotPath() { return true; }

void CleanupHotPathWifi() {}

#endif

}  // namespace

std::string_view ToString(HotSendStatus status) {
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

struct Header {
  AE_REFLECT_MEMBERS(root_code, size, dev_code)
  std::uint8_t const root_code = 0x3;
  std::uint8_t const size = sizeof(std::uint8_t) + sizeof(std::int16_t);
  std::uint8_t const dev_code = 0x10;
};

ae::DataBuffer MakeTemperaturePayload(std::string const& temperature) {
  static constexpr auto header = Header{};

  auto message = ae::DataBuffer{};
  message.reserve(sizeof(header) + temperature.size());
  {
    auto archive =
        ae::seri::BinaryArchive{ae::seri::BinaryVectorBuffer<>{message}};
    archive.Save(header);
    archive.Save(temperature);
  }
  return message;
}

bool HasPreparedSendBlock() { return g_prepared_send_message_block.is_valid(); }

std::uint32_t PreparedMessageLeft() {
  if (!g_prepared_send_message_block.is_valid()) {
    return 0;
  }
  return g_prepared_send_message_block.Resolve()->message_left;
}

void ClearPreparedSendBlock() {
  // magic indicate if block is valid
  // make it invalid
  g_prepared_send_message_block.raw.magic = {};
}

bool ExportPreparedSendBlock(ae::Client::ptr const& client, ae::Uid destination,
                             std::size_t reserve_message_count) {
  auto prep_res = ae::prepared_packet::PrepareSendMessageBlock(
      client, destination, reserve_message_count);

  if (!prep_res) {
    ESP_LOGE(kTag, "PrepareSendMessage failed ec=%d msg=%.*s",
             prep_res.error().ec,
             static_cast<int>(prep_res.error().msg.size()),
             prep_res.error().msg.data());
    return false;
  }

  g_prepared_send_message_block = std::move(prep_res).value();

  auto const resolved_block = g_prepared_send_message_block.Resolve();

  ESP_LOGI(kTag, "exported prepared block reserved %lu messages",
           static_cast<unsigned long>(resolved_block->message_left));
  return true;
}

ae::DataBuffer MakeBenchPayload(std::string_view kind, int sequence) {
  auto text = ae::Format("{}:{}", kind, sequence);
  return ae::DataBuffer{text.begin(), text.end()};
}

#if defined(ESP_PLATFORM)
void ReleaseFullAetherWifiForHotPath() {
  auto err = esp_wifi_stop();
  if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_STARTED &&
      err != ESP_ERR_WIFI_NOT_INIT) {
    ESP_LOGW(kTag, "esp_wifi_stop during release failed: %s",
             esp_err_to_name(err));
  }
  err = esp_wifi_deinit();
  if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_INIT) {
    ESP_LOGW(kTag, "esp_wifi_deinit during release failed: %s",
             esp_err_to_name(err));
  }
  if (esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
      netif != nullptr) {
    esp_netif_destroy(netif);
  }
  esp_netif_deinit();
  err = esp_event_loop_delete_default();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(kTag, "esp_event_loop_delete_default failed: %s",
             esp_err_to_name(err));
  }
  vTaskDelay(pdMS_TO_TICKS(50));
}
#endif

HotSendStatus SendPreparedOnce(ae::DataBuffer const& payload) {
#if defined(ESP_PLATFORM)
  if (!g_prepared_send_message_block.is_valid()) {
    return HotSendStatus::kNoPreparedBlock;
  }

  if (g_prepared_send_message_block.Resolve()->message_left == 0) {
    return HotSendStatus::kNonceExhausted;
  }

  if (!EnsureWifiConnectedForHotPath()) {
    WifiLifecyclePrintf("PREPARED_WIFI_FAIL reason=%s\n",
                        LastHotWifiFailReason());
    return HotSendStatus::kWifiFailed;
  }

  auto fail_after_wifi = ae_defer_at[] { CleanupHotPathWifi(); };

  ae::DataBuffer packet;
  auto encode_result = ae::prepared_packet::EncodePacket(
      g_prepared_send_message_block, payload, packet);

  if (!encode_result) {
    ClearPreparedSendBlock();
    return HotSendStatus::kEncodeFailed;
  }

  auto const resolved_block = g_prepared_send_message_block.Resolve();
  ESP_LOGI(kTag, "reserved messages left %lu",
           static_cast<unsigned long>(resolved_block->message_left));
  auto endpoint = resolved_block->endpoint;

  sockaddr_storage dest_storage{};
  socklen_t dest_len = 0;
  if (!FillUdpDestination(endpoint, reinterpret_cast<sockaddr*>(&dest_storage),
                          &dest_len)) {
    ESP_LOGE(kTag, "invalid endpoint address");
    return HotSendStatus::kSendFailed;
  }

  int sock = socket(
      endpoint.address.Index() == ae::AddrVersion::kIpV6 ? AF_INET6 : AF_INET,
      SOCK_DGRAM, IPPROTO_IP);
  if (sock < 0) {
    return HotSendStatus::kSendFailed;
  }

  auto sent = sendto(sock, packet.data(), packet.size(), 0,
                     reinterpret_cast<sockaddr*>(&dest_storage), dest_len);
  close(sock);

  if (sent != static_cast<ssize_t>(packet.size())) {
    return HotSendStatus::kSendFailed;
  }

#  ifndef AETHER_PREPARED_POST_SEND_HOLD_MS
#    define AETHER_PREPARED_POST_SEND_HOLD_MS 450
#  endif
  std::this_thread::sleep_for(
      std::chrono::milliseconds(AETHER_PREPARED_POST_SEND_HOLD_MS));

  ESP_LOGI(kTag, "hot path UDP sent %d bytes", static_cast<int>(sent));
  return HotSendStatus::kSent;
#else
  (void)payload;
  return HotSendStatus::kUnsupported;
#endif
}

HotSendStatus TryHotWakePreparedSend(
    [[maybe_unused]] std::string const& temperature) {
#if defined(ESP_PLATFORM)
  esp_reset_reason_t reset = esp_reset_reason();
  esp_sleep_wakeup_cause_t wakeup = esp_sleep_get_wakeup_cause();

  ESP_LOGI(kTag, "reset_reason=%d, wakeup_cause=%d", static_cast<int>(reset),
           static_cast<int>(wakeup));

  // Production deep-sleep semantics: only preserve local Wi-Fi RTC cache across
  // deep-sleep wakes. Cold / other resets invalidate the local prepared cache.
  if (reset != ESP_RST_DEEPSLEEP) {
    address_is_valid = false;
    bs_is_valid = false;
  }

  return SendPreparedOnce(MakeTemperaturePayload(temperature));
#else
  return HotSendStatus::kUnsupported;
#endif
}

#if defined(ESP_PLATFORM)
char const* LastHotWifiFailReason() { return g_last_hot_wifi_fail; }
#endif

}  // namespace temp_sensor::prepared_send
