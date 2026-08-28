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
#include "bench_payload.h"

#if !defined(ESP_PLATFORM)
#  include <iostream>
#endif

#if defined(ESP_PLATFORM)
#  include <esp_sleep.h>
#  include <esp_err.h>
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_mac.h>
#  include <esp_timer.h>
#  include <esp_wifi.h>
#  include <esp_wifi_default.h>
#  include <esp_private/wifi.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/event_groups.h>
#  include <lwip/inet.h>
#  include <lwip/sockets.h>
#  include <lwip/etharp.h>
#  include <lwip/netif.h>
#  include <lwip/tcpip.h>
#  include <esp_netif.h>
#  include <esp_netif_net_stack.h>
#  include <nvs_flash.h>
#  if !defined(AE_EXP_SILENT)
#    include <esp_log.h>
#  endif
#endif

namespace temp_sensor::prepared_send {

void ClearPreparedSendBlock();

#if defined(ESP_PLATFORM) && !defined(AE_EXP_SILENT)
static constexpr char const* kTag = "prepared-send";
#  define PS_LOGI(...) ESP_LOGI(kTag, __VA_ARGS__)
#  define PS_LOGW(...) ESP_LOGW(kTag, __VA_ARGS__)
#  define PS_LOGE(...) ESP_LOGE(kTag, __VA_ARGS__)
#  define PS_LOGD(...) ESP_LOGD(kTag, __VA_ARGS__)
#else
#  define PS_LOGI(...) \
    do {               \
    } while (0)
#  define PS_LOGW(...) \
    do {               \
    } while (0)
#  define PS_LOGE(...) \
    do {               \
    } while (0)
#  define PS_LOGD(...) \
    do {               \
    } while (0)
#endif

#ifndef AETHER_PREPARED_NONCE_RESERVE
#  define AETHER_PREPARED_NONCE_RESERVE 30
#endif

#ifndef AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS
#  define AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS 15000
#endif

#ifndef AETHER_PREPARED_HOT_WIFI_MAX_RETRY
#  define AETHER_PREPARED_HOT_WIFI_MAX_RETRY 10
#endif

#ifndef AETHER_PREPARED_ARP_TIMEOUT_MS
#  define AETHER_PREPARED_ARP_TIMEOUT_MS 500
#endif

static std::uint8_t g_last_send_cache_flags = 0;
static bool g_prepared_wifi_session_active = false;
static std::uint32_t g_last_wifi_session_start_us = 0;

#if defined(ESP_PLATFORM)
static RTC_NOINIT_ATTR ae::prepared_packet::PreparedSendMessageBlock
    g_prepared_send_message_block;

static RTC_DATA_ATTR esp_netif_ip_info_t rtc_ip_info = {};
static RTC_DATA_ATTR WiFiBaseStation base_station{};
static RTC_DATA_ATTR std::uint8_t gateway_mac[6] = {};
static RTC_NOINIT_ATTR bool address_is_valid;
static RTC_NOINIT_ATTR bool bs_is_valid;
static RTC_NOINIT_ATTR bool gateway_mac_valid;

static_assert(sizeof(ae::prepared_packet::PreparedSendMessageBlock) <= 8 * 1024,
              "PreparedSendMessageBlock must fit in ESP32 RTC slow memory");
#else
static ae::prepared_packet::PreparedSendMessageBlock g_prepared_send_message_block;
#endif

#if defined(ESP_PLATFORM)
void InvalidatePreparedWifiCache();  // defined below
#endif

namespace {

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
static bool g_wait_got_ip = true;
static bool g_using_bssid_cache = false;
static int g_max_wifi_retry = AETHER_PREPARED_HOT_WIFI_MAX_RETRY;
static constexpr EventBits_t kWifiReadyBit = BIT0;
static constexpr EventBits_t kWifiFailBit = BIT1;

void CaptureApIntoCache() {
  wifi_ap_record_t ap_info{};
  if (esp_wifi_sta_get_ap_info(&ap_info) != ESP_OK) {
    return;
  }
  base_station.target_channel = ap_info.primary;
  std::memcpy(base_station.target_bssid, ap_info.bssid,
              sizeof(base_station.target_bssid));
  bs_is_valid = true;
}

struct GatewayMacLookupCtx {
  struct netif* lwip_netif{nullptr};
  ip4_addr_t gw{};
  std::uint8_t mac[6]{};
  bool found{false};
};

esp_err_t LookupGatewayMacTcpip(void* arg) {
  auto* ctx = static_cast<GatewayMacLookupCtx*>(arg);
  struct eth_addr* eth_ret = nullptr;
  ip4_addr_t const* ip_ret = nullptr;
  auto const idx =
      etharp_find_addr(ctx->lwip_netif, &ctx->gw, &eth_ret, &ip_ret);
  if (idx >= 0 && eth_ret != nullptr) {
    std::memcpy(ctx->mac, eth_ret->addr, sizeof(ctx->mac));
    ctx->found = true;
  }
  return ESP_OK;
}

esp_err_t RequestGatewayArpTcpip(void* arg) {
  auto* ctx = static_cast<GatewayMacLookupCtx*>(arg);
  (void)etharp_request(ctx->lwip_netif, &ctx->gw);
  return ESP_OK;
}

struct StaticArpInstallCtx {
  ip4_addr_t gw{};
  struct eth_addr eth{};
  err_t err{ERR_VAL};
};

esp_err_t InstallStaticGatewayArpTcpip(void* arg) {
  auto* ctx = static_cast<StaticArpInstallCtx*>(arg);
  ctx->err = etharp_add_static_entry(&ctx->gw, &ctx->eth);
  return ESP_OK;
}

bool LookupGatewayMac(esp_netif_t* esp_netif, std::uint8_t out_mac[6]) {
  if (esp_netif == nullptr || rtc_ip_info.gw.addr == 0) {
    return false;
  }
  auto* lwip_netif =
      static_cast<struct netif*>(esp_netif_get_netif_impl(esp_netif));
  if (lwip_netif == nullptr) {
    return false;
  }

  GatewayMacLookupCtx ctx{};
  ctx.lwip_netif = lwip_netif;
  ctx.gw.addr = rtc_ip_info.gw.addr;
  if (esp_netif_tcpip_exec(&LookupGatewayMacTcpip, &ctx) != ESP_OK ||
      !ctx.found) {
    return false;
  }
  std::memcpy(out_mac, ctx.mac, 6);
  return true;
}

bool ResolveAndCacheGatewayMac(esp_netif_t* esp_netif) {
  std::uint8_t mac[6]{};
  if (LookupGatewayMac(esp_netif, mac)) {
    std::memcpy(gateway_mac, mac, sizeof(gateway_mac));
    gateway_mac_valid = true;
    return true;
  }

  auto* lwip_netif =
      static_cast<struct netif*>(esp_netif_get_netif_impl(esp_netif));
  if (lwip_netif == nullptr || rtc_ip_info.gw.addr == 0) {
    return false;
  }

  GatewayMacLookupCtx req{};
  req.lwip_netif = lwip_netif;
  req.gw.addr = rtc_ip_info.gw.addr;
  (void)esp_netif_tcpip_exec(&RequestGatewayArpTcpip, &req);

  auto const deadline =
      std::chrono::steady_clock::now() +
      std::chrono::milliseconds(AETHER_PREPARED_ARP_TIMEOUT_MS);
  while (std::chrono::steady_clock::now() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(20));
    if (LookupGatewayMac(esp_netif, mac)) {
      std::memcpy(gateway_mac, mac, sizeof(gateway_mac));
      gateway_mac_valid = true;
      return true;
    }
  }
  return false;
}

bool InstallStaticGatewayArp() {
  if (!gateway_mac_valid || !address_is_valid || rtc_ip_info.gw.addr == 0) {
    return false;
  }
  StaticArpInstallCtx ctx{};
  ctx.gw.addr = rtc_ip_info.gw.addr;
  std::memcpy(ctx.eth.addr, gateway_mac, sizeof(gateway_mac));
  if (esp_netif_tcpip_exec(&InstallStaticGatewayArpTcpip, &ctx) != ESP_OK) {
    return false;
  }
  return ctx.err == ERR_OK;
}

// Ensure gateway L2 destination is known before UDP sendto.
// Prefer cached MAC + static ARP; otherwise resolve via ARP (do not drop).
bool EnsureGatewayArpReady(esp_netif_t* esp_netif) {
  if (gateway_mac_valid && InstallStaticGatewayArp()) {
    g_last_send_cache_flags |=
        static_cast<std::uint8_t>(bench::CacheFlags::kUsedStaticArp);
    return true;
  }

  g_last_send_cache_flags |=
      static_cast<std::uint8_t>(bench::CacheFlags::kArpFallback);
  if (ResolveAndCacheGatewayMac(esp_netif)) {
    (void)InstallStaticGatewayArp();
  }
  // Do not discard the message if ARP is still unresolved; sendto may queue.
  return true;
}

void WifiEventHandler(void*, esp_event_base_t event_base, std::int32_t event_id,
                      void* event_data) {
  if (g_wifi_event_group == nullptr) {
    return;
  }

  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
    g_wifi_retry_count = 0;
    auto err = esp_wifi_connect();
    if (err != ESP_OK) {
      PS_LOGE("Wi-Fi hot path connect start failed: %s", esp_err_to_name(err));
      xEventGroupSetBits(g_wifi_event_group, kWifiFailBit);
    }
  } else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_CONNECTED) {
    CaptureApIntoCache();
    if (!g_wait_got_ip) {
      xEventGroupSetBits(g_wifi_event_group, kWifiReadyBit);
    }
  } else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_DISCONNECTED) {
    auto const* event =
        static_cast<wifi_event_sta_disconnected_t const*>(event_data);
    auto const reason = event != nullptr ? static_cast<int>(event->reason) : -1;

    if (g_wifi_retry_count < g_max_wifi_retry) {
      ++g_wifi_retry_count;
      PS_LOGW("Wi-Fi hot path disconnected reason=%d; retry %d/%d", reason,
              g_wifi_retry_count, g_max_wifi_retry);
      auto err = esp_wifi_connect();
      if (err != ESP_OK) {
        PS_LOGE("Wi-Fi hot path reconnect failed: %s", esp_err_to_name(err));
        xEventGroupSetBits(g_wifi_event_group, kWifiFailBit);
      }
    } else {
      PS_LOGE("Wi-Fi hot path retry limit reached; reason=%d", reason);
      xEventGroupSetBits(g_wifi_event_group, kWifiFailBit);
    }
  } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
    auto* event = static_cast<ip_event_got_ip_t*>(event_data);
    rtc_ip_info.ip = event->ip_info.ip;
    rtc_ip_info.netmask = event->ip_info.netmask;
    rtc_ip_info.gw = event->ip_info.gw;
    address_is_valid = true;
    CaptureApIntoCache();
    PS_LOGI("Wi-Fi hot path GOT_IP after %d retries", g_wifi_retry_count);
    xEventGroupSetBits(g_wifi_event_group, kWifiReadyBit);
  }
}

void CleanupHotPathWifiRuntime() {
  if (g_wifi_any_id_handler != nullptr) {
    esp_event_handler_instance_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                          g_wifi_any_id_handler);
    g_wifi_any_id_handler = nullptr;
  }

  if (g_wifi_got_ip_handler != nullptr) {
    esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                          g_wifi_got_ip_handler);
    g_wifi_got_ip_handler = nullptr;
  }

  if (g_wifi_started) {
    auto err = esp_wifi_stop();
    (void)err;
    g_wifi_started = false;
  }

  if (g_wifi_initialized) {
    auto err = esp_wifi_deinit();
    (void)err;
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
    esp_event_loop_delete_default();
    g_default_event_loop_created = false;
  }
}

bool StartWifiAttempt(bool use_bssid_cache, bool use_static_ip) {
#  ifndef WIFI_SSID
  return false;
#  endif
#  ifndef WIFI_PASSWORD
  return false;
#  endif

  CleanupHotPathWifiRuntime();

  g_wait_got_ip = !use_static_ip;
  g_using_bssid_cache = use_bssid_cache;
  g_max_wifi_retry = use_bssid_cache ? 1 : AETHER_PREPARED_HOT_WIFI_MAX_RETRY;

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  wifi_config_t wifi_config{};

  auto err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
      err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    err = nvs_flash_init();
  }
  if (err != ESP_OK && err != ESP_ERR_NVS_NO_FREE_PAGES) {
    return false;
  }

  (void)esp_netif_init();
  err = esp_event_loop_create_default();
  if (err == ESP_OK) {
    g_default_event_loop_created = true;
  } else if (err != ESP_ERR_INVALID_STATE) {
    return false;
  }

  g_wifi_event_group = xEventGroupCreate();
  if (g_wifi_event_group == nullptr) {
    CleanupHotPathWifiRuntime();
    return false;
  }

  g_wifi_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if (g_wifi_netif == nullptr) {
    g_wifi_netif = esp_netif_create_default_wifi_sta();
  }
  if (g_wifi_netif == nullptr) {
    CleanupHotPathWifiRuntime();
    return false;
  }

  if (use_static_ip && address_is_valid) {
    esp_netif_dhcpc_stop(g_wifi_netif);
    esp_netif_ip_info_t ip_info = {
        .ip = {.addr = rtc_ip_info.ip.addr},
        .netmask = {.addr = rtc_ip_info.netmask.addr},
        .gw = {.addr = rtc_ip_info.gw.addr}};
    esp_netif_set_ip_info(g_wifi_netif, &ip_info);
  }

  cfg.ampdu_rx_enable = 0;
  cfg.ampdu_tx_enable = 0;

  err = esp_wifi_init(&cfg);
  if (err == ESP_ERR_WIFI_INIT_STATE) {
    g_wifi_initialized = true;
  } else if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  } else {
    g_wifi_initialized = true;
  }

  err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                            &WifiEventHandler, nullptr,
                                            &g_wifi_any_id_handler);
  if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  }

  err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                            &WifiEventHandler, nullptr,
                                            &g_wifi_got_ip_handler);
  if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  }

  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.ssid), WIFI_SSID,
               sizeof(wifi_config.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.password), WIFI_PASSWORD,
               sizeof(wifi_config.sta.password));

  if (use_bssid_cache && bs_is_valid) {
    wifi_config.sta.scan_method = WIFI_FAST_SCAN;
    wifi_config.sta.bssid_set = true;
    wifi_config.sta.channel = base_station.target_channel;
    std::memcpy(wifi_config.sta.bssid, base_station.target_bssid,
                sizeof(base_station.target_bssid));
  }

  err = esp_wifi_set_mode(WIFI_MODE_STA);
  if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  }

  err = esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
  if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  }

  esp_wifi_set_protocol(
      WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

  err = esp_wifi_start();
  if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  }
  g_wifi_started = true;

  (void)esp_wifi_set_max_tx_power(80);
  (void)esp_wifi_set_ps(WIFI_PS_MAX_MODEM);

  EventBits_t bits = xEventGroupWaitBits(
      g_wifi_event_group, kWifiReadyBit | kWifiFailBit, pdFALSE, pdFALSE,
      pdMS_TO_TICKS(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS));

  esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true, (wifi_phy_rate_t)0x0);

  if ((bits & kWifiReadyBit) == 0) {
    return false;
  }

  // After DHCP (cold) or whenever IP is known, refresh gateway MAC cache.
  if (address_is_valid && g_wifi_netif != nullptr) {
    if (!gateway_mac_valid || !use_static_ip) {
      (void)ResolveAndCacheGatewayMac(g_wifi_netif);
    }
  }
  return true;
}

bool EnsureWifiConnectedForHotPath() {
  g_last_send_cache_flags = 0;

  bool const have_bssid = bs_is_valid;
  bool const have_ip = address_is_valid;

  if (have_bssid || have_ip) {
    if (StartWifiAttempt(have_bssid, have_ip)) {
      if (have_bssid) {
        g_last_send_cache_flags |=
            static_cast<std::uint8_t>(bench::CacheFlags::kUsedBssid);
      }
      if (have_ip) {
        g_last_send_cache_flags |=
            static_cast<std::uint8_t>(bench::CacheFlags::kUsedStaticIp) |
            static_cast<std::uint8_t>(bench::CacheFlags::kDhcpSkipped);
      }
      (void)EnsureGatewayArpReady(g_wifi_netif);
      return true;
    }

    // Cached BSSID/channel (and/or static IP) failed — invalidate and fall back.
    CleanupHotPathWifiRuntime();
    InvalidatePreparedWifiCache();
    g_last_send_cache_flags =
        static_cast<std::uint8_t>(bench::CacheFlags::kWifiFallback);
  }

  if (!StartWifiAttempt(/*use_bssid_cache=*/false, /*use_static_ip=*/false)) {
    CleanupHotPathWifiRuntime();
    return false;
  }
  (void)EnsureGatewayArpReady(g_wifi_netif);
  return true;
}

// Encode + socket/sendto/close only. Does not touch Wi-Fi lifetime.
HotSendStatus EncodeAndUdpSend(ae::DataBuffer const& payload) {
  if (!g_prepared_send_message_block.is_valid()) {
    return HotSendStatus::kNoPreparedBlock;
  }

  if (g_prepared_send_message_block.Resolve()->message_left == 0) {
    return HotSendStatus::kNonceExhausted;
  }

  ae::DataBuffer packet;
  auto encode_result = ae::prepared_packet::EncodePacket(
      g_prepared_send_message_block, payload, packet);

  if (!encode_result) {
    ClearPreparedSendBlock();
    return HotSendStatus::kEncodeFailed;
  }

  auto const resolved_block = g_prepared_send_message_block.Resolve();
  auto endpoint = resolved_block->endpoint;

  sockaddr_storage dest_storage{};
  socklen_t dest_len = 0;
  if (!FillUdpDestination(endpoint, reinterpret_cast<sockaddr*>(&dest_storage),
                          &dest_len)) {
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
  return HotSendStatus::kSent;
}

#else

bool EnsureWifiConnectedForHotPath() { return true; }

void CleanupHotPathWifiRuntime() {}

HotSendStatus EncodeAndUdpSend(ae::DataBuffer const&) {
  return HotSendStatus::kUnsupported;
}

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

std::uint8_t LastSendCacheFlags() { return g_last_send_cache_flags; }

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
  g_prepared_send_message_block.raw.magic = {};
}

bool ExportPreparedSendBlock(ae::Client::ptr const& client, ae::Uid destination,
                             std::size_t reserve_message_count) {
  auto prep_res = ae::prepared_packet::PrepareSendMessageBlock(
      client, destination, reserve_message_count);

  if (!prep_res) {
    PS_LOGE("PrepareSendMessage failed ec=%d", prep_res.error().ec);
    return false;
  }

  g_prepared_send_message_block = std::move(prep_res).value();
  return true;
}

ae::DataBuffer MakeBenchPayload(std::string_view kind, int sequence) {
  auto text = ae::Format("{}:{}", kind, sequence);
  return ae::DataBuffer{text.begin(), text.end()};
}

#if defined(ESP_PLATFORM)
void InvalidatePreparedWifiCache() {
  address_is_valid = false;
  bs_is_valid = false;
  gateway_mac_valid = false;
  std::memset(&rtc_ip_info, 0, sizeof(rtc_ip_info));
  std::memset(&base_station, 0, sizeof(base_station));
  std::memset(gateway_mac, 0, sizeof(gateway_mac));
}

bool CapturePreparedWifiCacheFromActiveConnection() {
  esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if (netif == nullptr) {
    return false;
  }

  esp_netif_ip_info_t ip_info{};
  if (esp_netif_get_ip_info(netif, &ip_info) != ESP_OK ||
      ip_info.ip.addr == 0) {
    return false;
  }

  rtc_ip_info = ip_info;
  address_is_valid = true;
  CaptureApIntoCache();
  if (!bs_is_valid) {
    return false;
  }

  // Prefer existing ARP entry from the live FULL session; request if needed.
  if (!ResolveAndCacheGatewayMac(netif)) {
    // Still keep BSSID/IP cache; prepared path can fall back to ARP wait.
    gateway_mac_valid = false;
  }
  return true;
}

void ReleaseFullAetherWifiForHotPath() {
  auto err = esp_wifi_stop();
  (void)err;
  err = esp_wifi_deinit();
  (void)err;
  if (esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
      netif != nullptr) {
    esp_netif_destroy(netif);
  }
  esp_netif_deinit();
  err = esp_event_loop_delete_default();
  (void)err;
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

  // Connect BEFORE EncodePacket so a failed Wi-Fi attempt does not burn a nonce.
  if (!EnsureWifiConnectedForHotPath()) {
    return HotSendStatus::kWifiFailed;
  }

  auto fail_after_wifi = ae_defer_at[] { CleanupHotPathWifiRuntime(); };

  auto const status = EncodeAndUdpSend(payload);
  if (status != HotSendStatus::kSent) {
    return status;
  }

#  ifndef AETHER_PREPARED_POST_SEND_HOLD_MS
#    define AETHER_PREPARED_POST_SEND_HOLD_MS 300
#  endif
  std::this_thread::sleep_for(
      std::chrono::milliseconds(AETHER_PREPARED_POST_SEND_HOLD_MS));

  return HotSendStatus::kSent;
#else
  (void)payload;
  return HotSendStatus::kUnsupported;
#endif
}

#if defined(ESP_PLATFORM)
void EndPreparedWifiSession();

bool BeginPreparedWifiSession() {
  if (g_prepared_wifi_session_active) {
    EndPreparedWifiSession();
  }
  g_last_wifi_session_start_us = 0;
  auto const t0 = esp_timer_get_time();
  if (!EnsureWifiConnectedForHotPath()) {
    return false;
  }
  auto const elapsed = esp_timer_get_time() - t0;
  g_last_wifi_session_start_us =
      elapsed < 0 ? 0
                  : static_cast<std::uint32_t>(
                        elapsed > static_cast<std::int64_t>(
                                      std::numeric_limits<std::uint32_t>::max())
                            ? std::numeric_limits<std::uint32_t>::max()
                            : elapsed);
  g_prepared_wifi_session_active = true;
  return true;
}

HotSendStatus SendPreparedPacketOnActiveWifi(ae::DataBuffer const& payload) {
  if (!g_prepared_wifi_session_active) {
    return HotSendStatus::kWifiFailed;
  }
  return EncodeAndUdpSend(payload);
}

void EndPreparedWifiSession() {
  CleanupHotPathWifiRuntime();
  g_prepared_wifi_session_active = false;
}

std::uint32_t LastWifiSessionStartUs() { return g_last_wifi_session_start_us; }
#endif

HotSendStatus TryHotWakePreparedSend(
    [[maybe_unused]] std::string const& temperature) {
#if defined(ESP_PLATFORM)
  esp_reset_reason_t reset = esp_reset_reason();

  // Production deep-sleep semantics: only preserve local Wi-Fi RTC cache across
  // deep-sleep wakes. Cold / other resets invalidate the local prepared cache.
  if (reset != ESP_RST_DEEPSLEEP) {
    InvalidatePreparedWifiCache();
  }

  return SendPreparedOnce(MakeTemperaturePayload(temperature));
#else
  return HotSendStatus::kUnsupported;
#endif
}

}  // namespace temp_sensor::prepared_send
