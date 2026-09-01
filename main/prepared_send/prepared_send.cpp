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
#include <atomic>
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
#  include <esp_system.h>
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

#if defined(ESP_PLATFORM)
// Per hot-cycle Wi-Fi event counters (reset at StartFastWifi).
std::atomic<std::uint32_t> g_wifi_disconnect_count{0};
std::atomic<std::uint8_t> g_wifi_last_disconnect_reason{0};
std::atomic<std::uint32_t> g_wifi_reconnect_count{0};
std::atomic<std::uint8_t> g_wifi_sta_connected_seen{0};
std::atomic<std::uint8_t> g_wifi_got_ip_seen{0};
static std::uint8_t g_last_used_static_arp = 0;
static std::uint8_t g_last_arp_fallback = 0;
#endif
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
static std::uint32_t g_last_wifi_init_us = 0;
static std::uint32_t g_last_heap_before_wifi = 0;
static std::uint32_t g_last_heap_after_wifi = 0;
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
    g_wifi_sta_connected_seen.store(1, std::memory_order_relaxed);
    CaptureApIntoCache();
    if (!g_wait_got_ip) {
      xEventGroupSetBits(g_wifi_event_group, kWifiReadyBit);
    }
  } else if (event_base == WIFI_EVENT &&
             event_id == WIFI_EVENT_STA_DISCONNECTED) {
    auto const* event =
        static_cast<wifi_event_sta_disconnected_t const*>(event_data);
    auto const reason = event != nullptr ? static_cast<int>(event->reason) : -1;
    g_wifi_disconnect_count.fetch_add(1, std::memory_order_relaxed);
    if (reason >= 0 && reason <= 255) {
      g_wifi_last_disconnect_reason.store(static_cast<std::uint8_t>(reason),
                                          std::memory_order_relaxed);
    }

    if (g_wifi_retry_count < g_max_wifi_retry) {
      ++g_wifi_retry_count;
      g_wifi_reconnect_count.fetch_add(1, std::memory_order_relaxed);
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
    g_wifi_got_ip_seen.store(1, std::memory_order_relaxed);
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

void CleanupHotPathWifiRuntime(std::uint8_t teardown_policy) {
  if (teardown_policy == 2) {
    return;
  }

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

  if (teardown_policy == 1) {
    return;
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

#if defined(ESP_PLATFORM)
// Late TX-done diagnostic state (fixed-size; no heap/log in callback).
struct TxDoneDiag {
  std::atomic<std::uint32_t> total{0};
  std::atomic<std::uint32_t> success{0};
  std::atomic<std::uint32_t> failed{0};
  std::atomic<std::int64_t> first_cb_us{0};
  std::atomic<std::int64_t> first_success_us{0};
  std::atomic<std::int64_t> first_failed_us{0};
  std::atomic<std::int64_t> last_cb_us{0};
  std::atomic<int> first_status{-1};  // -1 none, 0 fail, 1 success
};

std::atomic<bool> g_fast_tx_done_seen{false};      // any callback
std::atomic<bool> g_fast_tx_done_success{false};   // first success
std::atomic<int> g_fast_cb_count{0};
std::atomic<int> g_tx_wait_mode{0};  // FastTxDoneWaitMode as int
TxDoneDiag g_tx_diag{};

// Arming state for one datagram. The callback is registered immediately before
// sendto() and disarmed as soon as the wait is over, so a callback belonging to
// a previous datagram, to a retransmission, or to any other frame the driver
// sends cannot be credited to this one.
std::atomic<std::uint32_t> g_tx_generation{0};
std::atomic<std::uint32_t> g_tx_armed_generation{0};
std::atomic<bool> g_tx_armed{false};
std::atomic<std::int64_t> g_tx_sendto_begin_us{0};
static TaskHandle_t g_tx_done_wait_task{nullptr};

void ResetTxDoneDiag() {
  g_tx_diag.total.store(0, std::memory_order_relaxed);
  g_tx_diag.success.store(0, std::memory_order_relaxed);
  g_tx_diag.failed.store(0, std::memory_order_relaxed);
  g_tx_diag.first_cb_us.store(0, std::memory_order_relaxed);
  g_tx_diag.first_success_us.store(0, std::memory_order_relaxed);
  g_tx_diag.first_failed_us.store(0, std::memory_order_relaxed);
  g_tx_diag.last_cb_us.store(0, std::memory_order_relaxed);
  g_tx_diag.first_status.store(-1, std::memory_order_relaxed);
  g_fast_tx_done_seen.store(false, std::memory_order_release);
  g_fast_tx_done_success.store(false, std::memory_order_release);
  g_fast_cb_count.store(0, std::memory_order_relaxed);
}

void FastTxDoneCb(std::uint8_t, std::uint8_t*, std::uint16_t*, bool txStatus) {
  auto const now = esp_timer_get_time();
  if (!g_tx_armed.load(std::memory_order_acquire)) {
    return;
  }
  g_tx_diag.total.fetch_add(1, std::memory_order_relaxed);
  g_fast_cb_count.fetch_add(1, std::memory_order_relaxed);
  g_tx_diag.last_cb_us.store(now, std::memory_order_relaxed);

  if (g_tx_armed_generation.load(std::memory_order_relaxed) !=
      g_tx_generation.load(std::memory_order_relaxed)) {
    return;
  }

  int expected_first = -1;
  if (g_tx_diag.first_status.compare_exchange_strong(
          expected_first, txStatus ? 1 : 0, std::memory_order_relaxed)) {
    g_tx_diag.first_cb_us.store(now, std::memory_order_relaxed);
  }

  if (txStatus) {
    g_tx_diag.success.fetch_add(1, std::memory_order_relaxed);
    // A success only belongs to this datagram when it was raised after the
    // sendto() that carried it started.
    if (now >= g_tx_sendto_begin_us.load(std::memory_order_relaxed)) {
      std::int64_t expected_fs = 0;
      if (g_tx_diag.first_success_us.compare_exchange_strong(
              expected_fs, now, std::memory_order_relaxed)) {
        g_fast_tx_done_success.store(true, std::memory_order_release);
        if (g_tx_done_wait_task != nullptr) {
          xTaskNotifyGive(g_tx_done_wait_task);
        }
      }
    }
  } else {
    g_tx_diag.failed.fetch_add(1, std::memory_order_relaxed);
    std::int64_t expected_ff = 0;
    (void)g_tx_diag.first_failed_us.compare_exchange_strong(
        expected_ff, now, std::memory_order_relaxed);
  }

  g_fast_tx_done_seen.store(true, std::memory_order_release);
}

void ResetFastTxDone() {
  g_tx_armed.store(false, std::memory_order_release);
  ResetTxDoneDiag();
}

// Opens the window in which a TX-done callback may be credited to the datagram
// about to be handed to sendto(). Nothing but the sendto() call itself belongs
// between this and the syscall.
std::uint32_t ArmFastTxDone() {
  auto const generation =
      g_tx_generation.fetch_add(1, std::memory_order_relaxed) + 1;
  // Until the real sendto start is known no callback can qualify.
  g_tx_sendto_begin_us.store(std::numeric_limits<std::int64_t>::max(),
                             std::memory_order_relaxed);
  g_tx_armed_generation.store(generation, std::memory_order_relaxed);
  g_tx_armed.store(true, std::memory_order_release);
  return generation;
}

void MarkFastTxDoneSendtoBegin(std::int64_t begin_us) {
  g_tx_sendto_begin_us.store(begin_us, std::memory_order_release);
}

void DisarmFastTxDone() { g_tx_armed.store(false, std::memory_order_release); }

// Durations are reported as plain non-negative microseconds; a measurement that
// came out negative is a clock artefact, not a real interval.
static std::uint32_t NonNegativeUs(std::int64_t us) {
  if (us <= 0) {
    return 0;
  }
  return us > 0xffffffffll ? 0xffffffffu : static_cast<std::uint32_t>(us);
}

// The TX-done callback can precede the sendto() return, so this difference is
// genuinely signed.
static std::int32_t ClampToInt32(std::int64_t value) {
  constexpr auto kMax =
      static_cast<std::int64_t>(std::numeric_limits<std::int32_t>::max());
  constexpr auto kMin =
      static_cast<std::int64_t>(std::numeric_limits<std::int32_t>::min());
  if (value > kMax) {
    return std::numeric_limits<std::int32_t>::max();
  }
  if (value < kMin) {
    return std::numeric_limits<std::int32_t>::min();
  }
  return static_cast<std::int32_t>(value);
}

static std::uint32_t DeltaOrMissing(std::int64_t abs_us,
                                    std::int64_t sendto_return_us) {
  if (abs_us <= 0) {
    return 0xffffffffu;
  }
  auto const d = abs_us - sendto_return_us;
  if (d < 0) {
    return 0xffffffffu;
  }
  return d > 0xffffffffll ? 0xffffffffu : static_cast<std::uint32_t>(d);
}

static void FillTxDoneTiming(FastSendResult* timing, std::int64_t sendto_return_us,
                             bool condition_met, std::uint8_t after_success,
                             FastTxDoneWaitMode wait_mode) {
  if (timing == nullptr) {
    return;
  }
  auto const total = g_tx_diag.total.load(std::memory_order_relaxed);
  auto const success = g_tx_diag.success.load(std::memory_order_relaxed);
  auto const failed = g_tx_diag.failed.load(std::memory_order_relaxed);
  auto const first_st = g_tx_diag.first_status.load(std::memory_order_relaxed);
  timing->diag_mode = static_cast<std::uint8_t>(wait_mode);
  timing->tx_cb_total = total > 255 ? 255 : static_cast<std::uint8_t>(total);
  timing->tx_cb_success =
      success > 255 ? 255 : static_cast<std::uint8_t>(success);
  timing->tx_cb_failed = failed > 255 ? 255 : static_cast<std::uint8_t>(failed);
  timing->first_status =
      first_st < 0 ? 0xff : static_cast<std::uint8_t>(first_st);
  timing->first_cb_delta_us = DeltaOrMissing(
      g_tx_diag.first_cb_us.load(std::memory_order_relaxed), sendto_return_us);
  timing->first_success_delta_us = DeltaOrMissing(
      g_tx_diag.first_success_us.load(std::memory_order_relaxed),
      sendto_return_us);
  timing->first_failed_delta_us = DeltaOrMissing(
      g_tx_diag.first_failed_us.load(std::memory_order_relaxed),
      sendto_return_us);
  timing->last_cb_delta_us = DeltaOrMissing(
      g_tx_diag.last_cb_us.load(std::memory_order_relaxed), sendto_return_us);
  timing->callbacks_after_success = after_success;
  timing->cb_any = condition_met ? 1 : 0;
  timing->cb_timeout = condition_met ? 0 : 1;
  timing->cb_count = timing->tx_cb_total;
  timing->cb_match = 0;
}

HotSendStatus EncodeAndUdpSendTracked(ae::DataBuffer const& payload) {
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

// Encode → socket → set_tx_done_cb → sendto → wait condition → unset → close.
// MODE A (kFirstAny): wait first callback regardless of txStatus.
// MODE B (kFirstSuccess): wait first txStatus==true, then 5 ms observe window.
// Safety timeout: 100 ms from sendto return. Socket open until unregister.
#if defined(ESP_PLATFORM)
extern "C" esp_err_t esp_wifi_internal_set_retry_counter(uint8_t short_retry,
                                                         uint8_t long_retry);
#endif

// Prefer: EncodePacket + socket, then optional MAC retry counter, then
// ResetFastTxDone + tx-done cb + immediate sendto. No Wi-Fi ops between
// set_retry_counter and sendto. CONTROL leaves set_mac_retry_limit=false.
HotSendStatus SendHotArtifactsWithLateTxDone(HotSendArtifacts& artifacts,
                                             FastPathConfig const& cfg,
                                             FastSendResult* timing) {
  if (!artifacts.ready || artifacts.sock < 0) {
    return HotSendStatus::kSendFailed;
  }

  auto const wait_mode = cfg.tx_done_wait;
  auto const t_encode0 = esp_timer_get_time();

  if (timing != nullptr) {
    timing->mac_retry_called = 0;
    timing->mac_retry_set_rc = -1;
    timing->mac_short_retry = cfg.mac_short_retry;
    timing->mac_long_retry = cfg.mac_long_retry;
    timing->retry_cfg_us = 0;
  }
  if (cfg.set_mac_retry_limit) {
    auto const t_rc0 = esp_timer_get_time();
    esp_err_t const rc = esp_wifi_internal_set_retry_counter(
        cfg.mac_short_retry, cfg.mac_long_retry);
    auto const t_rc1 = esp_timer_get_time();
    if (timing != nullptr) {
      timing->mac_retry_called = 1;
      timing->mac_retry_set_rc = static_cast<std::int16_t>(rc);
      auto const d = t_rc1 - t_rc0;
      timing->retry_cfg_us = d < 0 ? 0 : static_cast<std::uint32_t>(d);
    }
  }

  ResetFastTxDone();
  g_tx_wait_mode.store(static_cast<int>(wait_mode), std::memory_order_relaxed);

  auto const t_cb_reg0 = esp_timer_get_time();
  esp_err_t const cb_rc = esp_wifi_set_tx_done_cb(&FastTxDoneCb);
  auto const t_cb_reg1 = esp_timer_get_time();
  if (timing != nullptr) {
    timing->encode_us = 0;
    timing->socket_create_us = 0;
    timing->callback_register_us = NonNegativeUs(t_cb_reg1 - t_cb_reg0);
    timing->tx_cb_register_rc = static_cast<std::int16_t>(cb_rc);
    timing->tx_cb_register_failed = cb_rc == ESP_OK ? 0 : 1;
  }

  (void)ArmFastTxDone();
  auto const t_sendto_begin = esp_timer_get_time();
  MarkFastTxDoneSendtoBegin(t_sendto_begin);
  auto sent = sendto(artifacts.sock, artifacts.packet.data(),
                     artifacts.packet.size(), 0,
                     reinterpret_cast<sockaddr*>(&artifacts.dest),
                     artifacts.dest_len);
  auto const t_send_ret = esp_timer_get_time();
  if (timing != nullptr) {
    auto const es = t_send_ret - t_encode0;
    timing->encode_send_us =
        es < 0 ? 0 : static_cast<std::uint32_t>(es);
    timing->sendto_begin_us = NonNegativeUs(t_sendto_begin);
    timing->sendto_return_us = NonNegativeUs(t_send_ret);
    timing->sendto_call_us = NonNegativeUs(t_send_ret - t_sendto_begin);
  }

  if (sent != static_cast<ssize_t>(artifacts.packet.size())) {
    DisarmFastTxDone();
    (void)esp_wifi_set_tx_done_cb(nullptr);
    CloseHotSendArtifacts(&artifacts);
    return HotSendStatus::kSendFailed;
  }

  bool const need_success = FastTxDoneRequiresSuccess(wait_mode);
  bool const can_observe = cb_rc == ESP_OK;
  constexpr std::int64_t kObserveUs = 5000;
  std::int64_t const kMaxUs =
      static_cast<std::int64_t>(cfg.tx_done_timeout_ms) * 1000;
  bool condition_met = false;
  std::uint32_t total_at_success = 0;

  auto condition_ready = [&]() -> bool {
    if (need_success) {
      return g_fast_tx_done_success.load(std::memory_order_acquire);
    }
    return g_fast_tx_done_seen.load(std::memory_order_acquire);
  };

  g_tx_done_wait_task = xTaskGetCurrentTaskHandle();
  if (condition_ready()) {
    condition_met = true;
  } else if (can_observe) {
    auto const wait_start = esp_timer_get_time();
    while ((esp_timer_get_time() - wait_start) < kMaxUs) {
      if (condition_ready()) {
        condition_met = true;
        break;
      }
      uint32_t notified = 0;
      auto const remaining_us = kMaxUs - (esp_timer_get_time() - wait_start);
      TickType_t const ticks =
          pdMS_TO_TICKS(remaining_us > 1000 ? remaining_us / 1000 : 1);
      if (xTaskNotifyWait(0, ULONG_MAX, &notified, ticks) == pdTRUE) {
        if (condition_ready()) {
          condition_met = true;
          break;
        }
      }
    }
  }
  g_tx_done_wait_task = nullptr;

  if (condition_met && wait_mode == FastTxDoneWaitMode::kFirstSuccess) {
    total_at_success = g_tx_diag.total.load(std::memory_order_relaxed);
    auto const t_obs0 = esp_timer_get_time();
    while ((esp_timer_get_time() - t_obs0) < kObserveUs) {
      uint32_t notified = 0;
      (void)xTaskNotifyWait(0, ULONG_MAX, &notified, pdMS_TO_TICKS(1));
    }
  }

  auto const t_cb_done = esp_timer_get_time();
  DisarmFastTxDone();
  (void)esp_wifi_set_tx_done_cb(nullptr);
  CloseHotSendArtifacts(&artifacts);

  bool const tx_done_confirmed = condition_met;
  if (timing != nullptr) {
    FillTxDoneTiming(timing, t_send_ret, condition_met, total_at_success,
                     wait_mode);
    timing->tx_done_wait_us = NonNegativeUs(t_cb_done - t_send_ret);
  }

  if (need_success && !tx_done_confirmed) {
    return HotSendStatus::kSentTxUnconfirmed;
  }
  return HotSendStatus::kSent;
}

HotSendStatus EncodeAndUdpSendWithLateTxDone(ae::DataBuffer const& payload,
                                             FastSendResult* timing,
                                             FastPathConfig const& cfg) {
  auto const wait_mode = cfg.tx_done_wait;
  if (!g_prepared_send_message_block.is_valid()) {
    return HotSendStatus::kNoPreparedBlock;
  }
  if (g_prepared_send_message_block.Resolve()->message_left == 0) {
    return HotSendStatus::kNonceExhausted;
  }

  auto const t_encode0 = esp_timer_get_time();

  ae::DataBuffer packet;
  auto encode_result = ae::prepared_packet::EncodePacket(
      g_prepared_send_message_block, payload, packet);
  if (!encode_result) {
    ClearPreparedSendBlock();
    return HotSendStatus::kEncodeFailed;
  }
  auto const t_encode1 = esp_timer_get_time();

  auto const resolved_block = g_prepared_send_message_block.Resolve();
  auto endpoint = resolved_block->endpoint;

  sockaddr_storage dest_storage{};
  socklen_t dest_len = 0;
  if (!FillUdpDestination(endpoint, reinterpret_cast<sockaddr*>(&dest_storage),
                          &dest_len)) {
    return HotSendStatus::kSendFailed;
  }

  auto const t_socket0 = esp_timer_get_time();
  int sock = socket(
      endpoint.address.Index() == ae::AddrVersion::kIpV6 ? AF_INET6 : AF_INET,
      SOCK_DGRAM, IPPROTO_IP);
  auto const t_socket1 = esp_timer_get_time();
  if (sock < 0) {
    return HotSendStatus::kSendFailed;
  }

  if (timing != nullptr) {
    timing->encode_us = NonNegativeUs(t_encode1 - t_encode0);
    timing->socket_create_us = NonNegativeUs(t_socket1 - t_socket0);
  }

  if (timing != nullptr) {
    timing->mac_retry_called = 0;
    timing->mac_retry_set_rc = -1;
    timing->mac_short_retry = cfg.mac_short_retry;
    timing->mac_long_retry = cfg.mac_long_retry;
    timing->retry_cfg_us = 0;
  }
  if (cfg.set_mac_retry_limit) {
    auto const t_rc0 = esp_timer_get_time();
    esp_err_t const rc = esp_wifi_internal_set_retry_counter(
        cfg.mac_short_retry, cfg.mac_long_retry);
    auto const t_rc1 = esp_timer_get_time();
    if (timing != nullptr) {
      timing->mac_retry_called = 1;
      timing->mac_retry_set_rc = static_cast<std::int16_t>(rc);
      auto const d = t_rc1 - t_rc0;
      timing->retry_cfg_us = d < 0 ? 0 : static_cast<std::uint32_t>(d);
    }
  }

  ResetFastTxDone();
  g_tx_wait_mode.store(static_cast<int>(wait_mode), std::memory_order_relaxed);

  auto const t_cb_reg0 = esp_timer_get_time();
  esp_err_t const cb_rc = esp_wifi_set_tx_done_cb(&FastTxDoneCb);
  auto const t_cb_reg1 = esp_timer_get_time();
  if (timing != nullptr) {
    timing->callback_register_us = NonNegativeUs(t_cb_reg1 - t_cb_reg0);
    timing->tx_cb_register_rc = static_cast<std::int16_t>(cb_rc);
    timing->tx_cb_register_failed = cb_rc == ESP_OK ? 0 : 1;
  }

  // Nothing but the two arming stores separates this from sendto(): no Wi-Fi
  // call, no ARP, no delay and no logging may be inserted here.
  (void)ArmFastTxDone();
  auto const t_sendto_begin = esp_timer_get_time();
  MarkFastTxDoneSendtoBegin(t_sendto_begin);
  auto sent = sendto(sock, packet.data(), packet.size(), 0,
                     reinterpret_cast<sockaddr*>(&dest_storage), dest_len);
  auto const t_send_ret = esp_timer_get_time();
  if (timing != nullptr) {
    auto const es = t_send_ret - t_encode0;
    timing->encode_send_us =
        es < 0 ? 0 : static_cast<std::uint32_t>(es);
    timing->sendto_begin_us = NonNegativeUs(t_sendto_begin);
    timing->sendto_return_us = NonNegativeUs(t_send_ret);
    timing->sendto_call_us = NonNegativeUs(t_send_ret - t_sendto_begin);
  }

  if (sent != static_cast<ssize_t>(packet.size())) {
    DisarmFastTxDone();
    (void)esp_wifi_set_tx_done_cb(nullptr);
    close(sock);
    return HotSendStatus::kSendFailed;
  }

  bool const need_success = FastTxDoneRequiresSuccess(wait_mode);
  // The registration failed, so no callback can be trusted and waiting for one
  // would only burn the whole timeout.
  bool const can_observe = cb_rc == ESP_OK;

  constexpr std::int64_t kObserveUs = 5000;
  std::int64_t const kMaxUs =
      static_cast<std::int64_t>(cfg.tx_done_timeout_ms) * 1000;
  bool condition_met = false;
  std::uint32_t total_at_success = 0;

  auto condition_ready = [&]() -> bool {
    if (need_success) {
      return g_fast_tx_done_success.load(std::memory_order_acquire);
    }
    return g_fast_tx_done_seen.load(std::memory_order_acquire);
  };

  g_tx_done_wait_task = xTaskGetCurrentTaskHandle();
  if (condition_ready()) {
    condition_met = true;
  } else if (can_observe) {
    auto const wait_start = esp_timer_get_time();
    while ((esp_timer_get_time() - wait_start) < kMaxUs) {
      if (condition_ready()) {
        condition_met = true;
        break;
      }
      uint32_t notified = 0;
      auto const remaining_us = kMaxUs - (esp_timer_get_time() - wait_start);
      TickType_t const ticks =
          pdMS_TO_TICKS(remaining_us > 1000 ? remaining_us / 1000 : 1);
      if (xTaskNotifyWait(0, ULONG_MAX, &notified, ticks) == pdTRUE) {
        if (condition_ready()) {
          condition_met = true;
          break;
        }
      }
    }
  }
  g_tx_done_wait_task = nullptr;

  // Legacy diagnostic observe window for kFirstSuccess only.
  // first success and must not be charged to a production send.
  if (condition_met && wait_mode == FastTxDoneWaitMode::kFirstSuccess) {
    total_at_success = g_tx_diag.total.load(std::memory_order_relaxed);
    auto const t_obs0 = esp_timer_get_time();
    while ((esp_timer_get_time() - t_obs0) < kObserveUs) {
      uint32_t notified = 0;
      (void)xTaskNotifyWait(0, ULONG_MAX, &notified, pdMS_TO_TICKS(1));
    }
  }

  auto const t_cb_done = esp_timer_get_time();
  DisarmFastTxDone();
  (void)esp_wifi_set_tx_done_cb(nullptr);
  close(sock);

  std::uint8_t after_success = 0;
  if (condition_met && wait_mode == FastTxDoneWaitMode::kFirstSuccess) {
    auto const total_end = g_tx_diag.total.load(std::memory_order_relaxed);
    auto const delta =
        total_end > total_at_success ? (total_end - total_at_success) : 0u;
    after_success = delta > 255 ? 255 : static_cast<std::uint8_t>(delta);
  }

  auto const first_success_us =
      g_tx_diag.first_success_us.load(std::memory_order_relaxed);
  bool const tx_done_confirmed = can_observe && first_success_us > 0;

  if (timing != nullptr) {
    auto const wait = t_cb_done - t_send_ret;
    timing->tx_done_wait_us =
        wait < 0 ? 0 : static_cast<std::uint32_t>(wait);
    timing->tx_done_confirmed = tx_done_confirmed ? 1 : 0;
    if (tx_done_confirmed) {
      timing->first_success_callback_us = NonNegativeUs(first_success_us);
      timing->send_to_txdone_us =
          NonNegativeUs(first_success_us - t_sendto_begin);
      timing->txdone_minus_sendto_return_us =
          ClampToInt32(first_success_us - t_send_ret);
    } else {
      timing->first_success_callback_us = 0;
      timing->send_to_txdone_us = kTimingMissing;
      timing->txdone_minus_sendto_return_us = 0;
    }
    FillTxDoneTiming(timing, t_send_ret, condition_met, after_success,
                     wait_mode);
  }

  // The datagram is on the air either way and its nonce is spent, but a
  // parameter must never be judged from a send whose transmission the driver
  // never confirmed.
  if (need_success && !tx_done_confirmed) {
    return HotSendStatus::kSentTxUnconfirmed;
  }
  return HotSendStatus::kSent;
}
#endif

#else

bool EnsureWifiConnectedForHotPath() { return true; }

void CleanupHotPathWifiRuntime(std::uint8_t /*teardown_policy*/) {}

HotSendStatus EncodeAndUdpSend(ae::DataBuffer const&) {
  return HotSendStatus::kUnsupported;
}

#endif

}  // namespace

std::string_view ToString(HotSendStatus status) {
  switch (status) {
    case HotSendStatus::kSent:
      return "sent";
    case HotSendStatus::kSentTxUnconfirmed:
      return "sent-tx-unconfirmed";
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

namespace {

struct BisectFactorConfig {
  bool use_bssid{false};
  bool use_channel{false};
  bool use_fast_scan{false};
  bool use_static_ip{false};
  bool use_static_arp{false};
  bool ps_max_modem{false};
  bool ampdu_off{false};
  bool fixed_1m{false};
  std::uint8_t pre_delay_ms{200};
};

BisectWifiCacheSnapshot g_bisect_cache{};
std::uint8_t g_bisect_actual_channel = 0;

BisectFactorConfig MakeBisectConfig(WifiBisectVariant variant) {
  BisectFactorConfig c{};
  switch (variant) {
    case WifiBisectVariant::kB0:
      c.pre_delay_ms = 0;
      break;
    case WifiBisectVariant::kB1:
      break;
    case WifiBisectVariant::kC1:
      c.use_bssid = true;
      break;
    case WifiBisectVariant::kC2:
      c.use_channel = true;
      break;
    case WifiBisectVariant::kC3:
      c.use_bssid = true;
      c.use_channel = true;
      break;
    case WifiBisectVariant::kC4:
      c.use_fast_scan = true;
      break;
    case WifiBisectVariant::kC5:
      c.use_static_ip = true;
      break;
    case WifiBisectVariant::kC6:
      c.use_static_ip = true;
      c.use_static_arp = true;
      break;
    case WifiBisectVariant::kC7:
      c.use_bssid = true;
      c.use_static_ip = true;
      break;
    case WifiBisectVariant::kC8:
      c.use_channel = true;
      c.use_static_ip = true;
      break;
    case WifiBisectVariant::kP1:
      c.ps_max_modem = true;
      break;
    case WifiBisectVariant::kP2:
      c.ampdu_off = true;
      break;
    case WifiBisectVariant::kP3:
      c.fixed_1m = true;
      break;
    case WifiBisectVariant::kCount:
      break;
  }
  return c;
}

std::uint8_t BisectFactorBitsOf(BisectFactorConfig const& c) {
  using F = bench::BisectFactorBits;
  std::uint8_t bits = 0;
  if (c.use_bssid) {
    bits |= static_cast<std::uint8_t>(F::kBssid);
  }
  if (c.use_channel) {
    bits |= static_cast<std::uint8_t>(F::kChannel);
  }
  if (c.use_fast_scan) {
    bits |= static_cast<std::uint8_t>(F::kFastScan);
  }
  if (c.use_static_ip) {
    bits |= static_cast<std::uint8_t>(F::kStaticIp);
  }
  if (c.use_static_arp) {
    bits |= static_cast<std::uint8_t>(F::kStaticArp);
  }
  if (c.ps_max_modem) {
    bits |= static_cast<std::uint8_t>(F::kPsMaxModem);
  }
  if (c.ampdu_off) {
    bits |= static_cast<std::uint8_t>(F::kAmpduOff);
  }
  if (c.fixed_1m) {
    bits |= static_cast<std::uint8_t>(F::kFixed1M);
  }
  return bits;
}

std::uint8_t ReadActualChannel() {
  wifi_ap_record_t ap_info{};
  if (esp_wifi_sta_get_ap_info(&ap_info) != ESP_OK) {
    return 0;
  }
  return ap_info.primary;
}

bool StartBisectWifi(BisectFactorConfig const& cfg) {
#  ifndef WIFI_SSID
  return false;
#  endif
#  ifndef WIFI_PASSWORD
  return false;
#  endif

  CleanupHotPathWifiRuntime();
  g_bisect_actual_channel = 0;

  bool const need_static_ip = cfg.use_static_ip && g_bisect_cache.valid_ip;
  g_wait_got_ip = !need_static_ip;
  g_using_bssid_cache = cfg.use_bssid && g_bisect_cache.valid_bssid;
  g_max_wifi_retry = AETHER_PREPARED_HOT_WIFI_MAX_RETRY;

  wifi_init_config_t wifi_init_cfg = WIFI_INIT_CONFIG_DEFAULT();
  if (cfg.ampdu_off) {
    wifi_init_cfg.ampdu_rx_enable = 0;
    wifi_init_cfg.ampdu_tx_enable = 0;
  }

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

  if (need_static_ip) {
    esp_netif_dhcpc_stop(g_wifi_netif);
    esp_netif_ip_info_t ip_info = {
        .ip = {.addr = g_bisect_cache.ip},
        .netmask = {.addr = g_bisect_cache.netmask},
        .gw = {.addr = g_bisect_cache.gateway}};
    esp_netif_set_ip_info(g_wifi_netif, &ip_info);
    rtc_ip_info = ip_info;
    address_is_valid = true;
  }

  err = esp_wifi_init(&wifi_init_cfg);
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

  wifi_config_t wifi_config{};
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.ssid), WIFI_SSID,
               sizeof(wifi_config.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.password), WIFI_PASSWORD,
               sizeof(wifi_config.sta.password));
  wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA3_PSK;

  if (cfg.use_fast_scan) {
    wifi_config.sta.scan_method = WIFI_FAST_SCAN;
  }

  if (cfg.use_bssid && g_bisect_cache.valid_bssid) {
    wifi_config.sta.bssid_set = true;
    std::memcpy(wifi_config.sta.bssid, g_bisect_cache.bssid,
                sizeof(wifi_config.sta.bssid));
  }

  if (cfg.use_channel && g_bisect_cache.valid_bssid) {
    wifi_config.sta.channel = g_bisect_cache.channel;
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

  (void)esp_wifi_set_protocol(
      WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

  err = esp_wifi_start();
  if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  }
  g_wifi_started = true;

  (void)esp_wifi_set_max_tx_power(80);
  (void)esp_wifi_set_ps(cfg.ps_max_modem ? WIFI_PS_MAX_MODEM : WIFI_PS_NONE);

  EventBits_t bits = xEventGroupWaitBits(
      g_wifi_event_group, kWifiReadyBit | kWifiFailBit, pdFALSE, pdFALSE,
      pdMS_TO_TICKS(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS));

  if (cfg.fixed_1m) {
    (void)esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true,
                                         WIFI_PHY_RATE_1M_L);
  }

  if ((bits & kWifiReadyBit) == 0) {
    return false;
  }

  g_bisect_actual_channel = ReadActualChannel();

  if (cfg.use_static_arp && g_bisect_cache.valid_gw_mac &&
      g_bisect_cache.valid_ip) {
    std::memcpy(gateway_mac, g_bisect_cache.gw_mac, sizeof(gateway_mac));
    gateway_mac_valid = true;
    (void)InstallStaticGatewayArp();
  }

  return true;
}

void ApplyConnectedPsMode(FastPathConfig const& cfg) {
  if (cfg.ps_max_modem || cfg.connected_ps_mode == 2) {
    (void)esp_wifi_set_ps(WIFI_PS_MAX_MODEM);
  } else if (cfg.connected_ps_mode == 1) {
    (void)esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
  } else {
    (void)esp_wifi_set_ps(WIFI_PS_NONE);
  }
}

void ApplyPhasePsBeforeTx(FastPathConfig const& cfg) {
  if (cfg.phase_ps == 3 || cfg.phase_ps == 4) {
    (void)esp_wifi_set_ps(WIFI_PS_NONE);
  }
}

void WaitUntilPreDeadline(std::int64_t ready_us, FastPathConfig const& cfg) {
  if (cfg.pre_delay_ms == 0) {
    return;
  }
  auto const deadline =
      ready_us + static_cast<std::int64_t>(cfg.pre_delay_ms) * 1000;
  while (esp_timer_get_time() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

struct HotSendArtifacts {
  ae::DataBuffer packet{};
  int sock{-1};
  sockaddr_storage dest{};
  socklen_t dest_len{0};
  bool ready{false};
};

void CloseHotSendArtifacts(HotSendArtifacts* artifacts) {
  if (artifacts == nullptr || artifacts->sock < 0) {
    return;
  }
  close(artifacts->sock);
  artifacts->sock = -1;
}

HotSendStatus PrepareHotSendArtifacts(ae::DataBuffer const& payload,
                                      HotSendArtifacts* artifacts) {
  if (artifacts == nullptr) {
    return HotSendStatus::kSendFailed;
  }
  *artifacts = HotSendArtifacts{};
  if (!g_prepared_send_message_block.is_valid()) {
    return HotSendStatus::kNoPreparedBlock;
  }
  if (g_prepared_send_message_block.Resolve()->message_left == 0) {
    return HotSendStatus::kNonceExhausted;
  }

  auto encode_result = ae::prepared_packet::EncodePacket(
      g_prepared_send_message_block, payload, artifacts->packet);
  if (!encode_result) {
    ClearPreparedSendBlock();
    return HotSendStatus::kEncodeFailed;
  }

  auto const resolved_block = g_prepared_send_message_block.Resolve();
  auto endpoint = resolved_block->endpoint;
  if (!FillUdpDestination(endpoint,
                          reinterpret_cast<sockaddr*>(&artifacts->dest),
                          &artifacts->dest_len)) {
    return HotSendStatus::kSendFailed;
  }

  artifacts->sock = socket(
      endpoint.address.Index() == ae::AddrVersion::kIpV6 ? AF_INET6 : AF_INET,
      SOCK_DGRAM, IPPROTO_IP);
  if (artifacts->sock < 0) {
    return HotSendStatus::kSendFailed;
  }
  artifacts->ready = true;
  return HotSendStatus::kSent;
}

bool FinishFastWifiAssociation(FastPathConfig const& cfg,
                               BisectWifiCacheSnapshot const& cache) {
  EventBits_t bits = xEventGroupWaitBits(
      g_wifi_event_group, kWifiReadyBit | kWifiFailBit, pdFALSE, pdFALSE,
      pdMS_TO_TICKS(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS));

  if (cfg.fixed_1m) {
    (void)esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true, WIFI_PHY_RATE_1M_L);
  }

  if ((bits & kWifiReadyBit) == 0) {
    return false;
  }

  g_bisect_actual_channel = ReadActualChannel();

  bool used_static_arp = false;
  bool arp_fallback = false;
  if (cfg.use_static_arp && cache.valid_gw_mac && cache.valid_ip) {
    std::memcpy(gateway_mac, cache.gw_mac, sizeof(gateway_mac));
    gateway_mac_valid = true;
    if (InstallStaticGatewayArp()) {
      used_static_arp = true;
    } else {
      gateway_mac_valid = false;
    }
  }
  if (!gateway_mac_valid && g_wifi_netif != nullptr &&
      (cfg.use_static_arp || cfg.arp_wait_on_miss)) {
    arp_fallback = cfg.use_static_arp ? true : arp_fallback;
    if (!used_static_arp) {
      arp_fallback = true;
    }
    auto const t_arp0 = esp_timer_get_time();
    while ((esp_timer_get_time() - t_arp0) <
           static_cast<std::int64_t>(AETHER_PREPARED_ARP_TIMEOUT_MS) * 1000) {
      if (ResolveAndCacheGatewayMac(g_wifi_netif)) {
        (void)InstallStaticGatewayArp();
        break;
      }
      vTaskDelay(pdMS_TO_TICKS(50));
    }
  }
  g_last_used_static_arp = used_static_arp ? 1 : 0;
  g_last_arp_fallback = arp_fallback ? 1 : 0;
  return true;
}

std::uint8_t ReadNegotiatedAuth() {
  wifi_ap_record_t ap_info{};
  if (esp_wifi_sta_get_ap_info(&ap_info) != ESP_OK) {
    return 0;
  }
  return static_cast<std::uint8_t>(ap_info.authmode);
}

bool StartFastWifi(FastPathConfig const& cfg,
                   BisectWifiCacheSnapshot const* cache_override,
                   bool wait_ready = true) {
#  ifndef WIFI_SSID
  return false;
#  endif
#  ifndef WIFI_PASSWORD
  return false;
#  endif

  BisectWifiCacheSnapshot const& cache =
      cache_override != nullptr ? *cache_override : g_bisect_cache;

  CleanupHotPathWifiRuntime();
  g_bisect_actual_channel = 0;
  g_wifi_disconnect_count.store(0, std::memory_order_relaxed);
  g_wifi_last_disconnect_reason.store(0, std::memory_order_relaxed);
  g_wifi_reconnect_count.store(0, std::memory_order_relaxed);
  g_wifi_sta_connected_seen.store(0, std::memory_order_relaxed);
  g_wifi_got_ip_seen.store(0, std::memory_order_relaxed);

  bool const need_static_ip = cfg.use_static_ip && cache.valid_ip;
  g_wait_got_ip = !need_static_ip;
  g_using_bssid_cache = cfg.use_bssid && cache.valid_bssid;
  g_max_wifi_retry = cfg.retry_max;

  wifi_init_config_t wifi_init_cfg = WIFI_INIT_CONFIG_DEFAULT();
  if (cfg.ampdu_tx_off) {
    wifi_init_cfg.ampdu_tx_enable = 0;
  }
  if (cfg.ampdu_rx_off) {
    wifi_init_cfg.ampdu_rx_enable = 0;
    wifi_init_cfg.rx_ba_win = 0;
  } else if (cfg.rx_ba_win != 0) {
    wifi_init_cfg.rx_ba_win = cfg.rx_ba_win;
  }
  if (cfg.amsdu_tx_off) {
    wifi_init_cfg.amsdu_tx_enable = 0;
  }
  wifi_init_cfg.nvs_enable = cfg.wifi_nvs_enable ? 1 : 0;
  if (cfg.static_rx_buf_num != 0) {
    wifi_init_cfg.static_rx_buf_num = cfg.static_rx_buf_num;
  }
  if (cfg.dynamic_rx_buf_num != 0) {
    wifi_init_cfg.dynamic_rx_buf_num = cfg.dynamic_rx_buf_num;
  }
  if (cfg.dynamic_tx_buf_num != 0) {
    wifi_init_cfg.dynamic_tx_buf_num = cfg.dynamic_tx_buf_num;
  }

  auto const heap_before = esp_get_free_heap_size();
  auto const t_wifi_init0 = esp_timer_get_time();

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

  if (need_static_ip) {
    esp_netif_dhcpc_stop(g_wifi_netif);
    esp_netif_ip_info_t ip_info = {
        .ip = {.addr = cache.ip},
        .netmask = {.addr = cache.netmask},
        .gw = {.addr = cache.gateway}};
    esp_netif_set_ip_info(g_wifi_netif, &ip_info);
    rtc_ip_info = ip_info;
    address_is_valid = true;
  }

  err = esp_wifi_init(&wifi_init_cfg);
  auto const t_wifi_init1 = esp_timer_get_time();
  g_last_wifi_init_us =
      (t_wifi_init1 > t_wifi_init0)
          ? static_cast<std::uint32_t>(t_wifi_init1 - t_wifi_init0)
          : 0;
  g_last_heap_before_wifi = heap_before;
  g_last_heap_after_wifi = esp_get_free_heap_size();
  if (err == ESP_ERR_WIFI_INIT_STATE) {
    g_wifi_initialized = true;
  } else if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  } else {
    g_wifi_initialized = true;
  }

  if (cfg.wifi_storage_ram) {
    (void)esp_wifi_set_storage(WIFI_STORAGE_RAM);
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

  wifi_config_t wifi_config{};
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.ssid), WIFI_SSID,
               sizeof(wifi_config.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wifi_config.sta.password), WIFI_PASSWORD,
               sizeof(wifi_config.sta.password));

  wifi_config.sta.pmf_cfg.capable = cfg.pmf_off ? false : true;
  if (cfg.auth == FastAuthMode::kWpa2) {
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.sae_pwe_h2e = WPA3_SAE_PWE_UNSPECIFIED;
    wifi_config.sta.pmf_cfg.required = false;
  } else if (cfg.auth == FastAuthMode::kWpa3H2eOnly) {
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA3_PSK;
    wifi_config.sta.sae_pwe_h2e = WPA3_SAE_PWE_HASH_TO_ELEMENT;
    wifi_config.sta.pmf_cfg.required = true;
  } else {
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA3_PSK;
    wifi_config.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    wifi_config.sta.pmf_cfg.required = true;
  }

  if (cfg.use_fast_scan) {
    wifi_config.sta.scan_method = WIFI_FAST_SCAN;
  }

  if (cfg.use_bssid && cache.valid_bssid) {
    wifi_config.sta.bssid_set = true;
    std::memcpy(wifi_config.sta.bssid, cache.bssid,
                sizeof(wifi_config.sta.bssid));
  }

  if (cfg.use_channel && cache.channel != 0) {
    wifi_config.sta.channel = cache.channel;
  }

  if (cfg.connected_ps_mode == 2 && cfg.listen_interval > 0) {
    wifi_config.sta.listen_interval = cfg.listen_interval;
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

  (void)esp_wifi_set_protocol(
      WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);

  err = esp_wifi_start();
  if (err != ESP_OK) {
    CleanupHotPathWifiRuntime();
    return false;
  }
  g_wifi_started = true;

  (void)esp_wifi_set_max_tx_power(80);
  if (cfg.force_ht20) {
    (void)esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20);
  }
  if (cfg.dynamic_cs == 0) {
    (void)esp_wifi_set_dynamic_cs(false);
  } else if (cfg.dynamic_cs == 1) {
    (void)esp_wifi_set_dynamic_cs(true);
  }
  ApplyConnectedPsMode(cfg);

  if (!wait_ready) {
    return true;
  }

  return FinishFastWifiAssociation(cfg, cache);
}

}  // namespace

bool FreezeBisectWifiCacheFromActiveConnection() {
  g_bisect_cache = {};
  esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if (netif == nullptr) {
    return false;
  }

  esp_netif_ip_info_t ip_info{};
  if (esp_netif_get_ip_info(netif, &ip_info) != ESP_OK ||
      ip_info.ip.addr == 0) {
    return false;
  }

  wifi_ap_record_t ap_info{};
  if (esp_wifi_sta_get_ap_info(&ap_info) != ESP_OK) {
    return false;
  }

  g_bisect_cache.valid_ip = true;
  g_bisect_cache.ip = ip_info.ip.addr;
  g_bisect_cache.netmask = ip_info.netmask.addr;
  g_bisect_cache.gateway = ip_info.gw.addr;

  g_bisect_cache.valid_bssid = true;
  g_bisect_cache.channel = ap_info.primary;
  std::memcpy(g_bisect_cache.bssid, ap_info.bssid,
              sizeof(g_bisect_cache.bssid));

  // Also refresh production RTC cache helpers used by ARP install.
  rtc_ip_info = ip_info;
  address_is_valid = true;
  CaptureApIntoCache();
  if (ResolveAndCacheGatewayMac(netif)) {
    g_bisect_cache.valid_gw_mac = true;
    std::memcpy(g_bisect_cache.gw_mac, gateway_mac,
                sizeof(g_bisect_cache.gw_mac));
  }
  return true;
}

BisectWifiCacheSnapshot GetBisectWifiCacheSnapshot() { return g_bisect_cache; }

BisectSendResult SendPreparedOnceWithBisectFactor(
    WifiBisectVariant variant, ae::DataBuffer const& payload) {
  BisectSendResult out{};
  auto const cfg = MakeBisectConfig(variant);
  out.pre_delay_ms = cfg.pre_delay_ms;
  out.factor_bits = BisectFactorBitsOf(cfg);
  out.requested_channel =
      (cfg.use_channel && g_bisect_cache.valid_bssid) ? g_bisect_cache.channel
                                                      : 0;

  if (!g_prepared_send_message_block.is_valid()) {
    out.status = HotSendStatus::kNoPreparedBlock;
    return out;
  }
  if (g_prepared_send_message_block.Resolve()->message_left == 0) {
    out.status = HotSendStatus::kNonceExhausted;
    return out;
  }

  auto const t0 = esp_timer_get_time();
  if (!StartBisectWifi(cfg)) {
    CleanupHotPathWifiRuntime();
    out.status = HotSendStatus::kWifiFailed;
    out.actual_channel = g_bisect_actual_channel;
    auto const elapsed = esp_timer_get_time() - t0;
    out.total_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
    return out;
  }

  out.status_flags |=
      static_cast<std::uint8_t>(bench::BisectStatusBits::kWifiReady);
  out.actual_channel = g_bisect_actual_channel;
  if (out.requested_channel != 0 &&
      out.requested_channel == out.actual_channel) {
    out.status_flags |=
        static_cast<std::uint8_t>(bench::BisectStatusBits::kChannelMatch);
  }

  if (cfg.pre_delay_ms > 0) {
    vTaskDelay(pdMS_TO_TICKS(cfg.pre_delay_ms));
  }

  auto const encode_status = EncodeAndUdpSend(payload);
  if (encode_status == HotSendStatus::kSent) {
    out.status_flags |=
        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk) |
        static_cast<std::uint8_t>(bench::BisectStatusBits::kSendtoOk);
  } else if (encode_status == HotSendStatus::kEncodeFailed) {
    // encode failed after wifi ready
  } else if (encode_status == HotSendStatus::kSendFailed) {
    out.status_flags |=
        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk);
  }

#  ifndef AETHER_PREPARED_POST_SEND_HOLD_MS
#    define AETHER_PREPARED_POST_SEND_HOLD_MS 300
#  endif
  if (encode_status == HotSendStatus::kSent) {
    vTaskDelay(pdMS_TO_TICKS(AETHER_PREPARED_POST_SEND_HOLD_MS));
  }

  CleanupHotPathWifiRuntime();

  auto const elapsed = esp_timer_get_time() - t0;
  out.total_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
  out.status = encode_status;
  return out;
}

FastSendResult SendPreparedOnceWithFastPath(
    FastPathConfig const& cfg, ae::DataBuffer const& payload,
    BisectWifiCacheSnapshot const* wifi_cache) {
  FastSendResult out{};
  BisectWifiCacheSnapshot const& cache =
      wifi_cache != nullptr ? *wifi_cache : g_bisect_cache;
  out.requested_channel =
      (cfg.use_channel && cache.channel != 0) ? cache.channel : 0;

  if (!g_prepared_send_message_block.is_valid()) {
    out.status = HotSendStatus::kNoPreparedBlock;
    return out;
  }
  if (g_prepared_send_message_block.Resolve()->message_left == 0) {
    out.status = HotSendStatus::kNonceExhausted;
    return out;
  }

  auto const t0 = esp_timer_get_time();
  HotSendArtifacts artifacts{};
  bool wifi_ready = false;
  std::int64_t t_ready = t0;

  if (cfg.encode_during_association) {
    if (!StartFastWifi(cfg, wifi_cache, false)) {
      CleanupHotPathWifiRuntime(cfg.teardown_policy);
      out.status = HotSendStatus::kWifiFailed;
      out.actual_channel = g_bisect_actual_channel;
      out.negotiated_auth = ReadNegotiatedAuth();
      auto const elapsed = esp_timer_get_time() - t0;
      out.cycle_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
      out.connect_us = out.cycle_us;
      out.fail_stage = 1;
      return out;
    }
    auto const assoc_deadline =
        t0 + static_cast<std::int64_t>(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS) *
                 1000;
    bool encoded = false;
    while (esp_timer_get_time() < assoc_deadline) {
      EventBits_t const bits = xEventGroupGetBits(g_wifi_event_group);
      if ((bits & kWifiFailBit) != 0) {
        break;
      }
      if ((bits & kWifiReadyBit) != 0) {
        wifi_ready = FinishFastWifiAssociation(cfg, cache);
        t_ready = esp_timer_get_time();
        break;
      }
      if (!encoded) {
        auto const prep = PrepareHotSendArtifacts(payload, &artifacts);
        if (prep == HotSendStatus::kEncodeFailed ||
            prep == HotSendStatus::kNonceExhausted ||
            prep == HotSendStatus::kNoPreparedBlock) {
          CleanupHotPathWifiRuntime(cfg.teardown_policy);
          out.status = prep;
          out.fail_stage = prep == HotSendStatus::kWifiFailed ? 1 : 2;
          auto const elapsed = esp_timer_get_time() - t0;
          out.cycle_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
          return out;
        }
        encoded = artifacts.ready;
      }
      vTaskDelay(pdMS_TO_TICKS(1));
    }
    if (!wifi_ready) {
      CloseHotSendArtifacts(&artifacts);
      CleanupHotPathWifiRuntime(cfg.teardown_policy);
      out.status = HotSendStatus::kWifiFailed;
      out.fail_stage = 1;
      out.actual_channel = g_bisect_actual_channel;
      out.negotiated_auth = ReadNegotiatedAuth();
      auto const elapsed = esp_timer_get_time() - t0;
      out.cycle_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
      out.connect_us = out.cycle_us;
      return out;
    }
  } else if (!StartFastWifi(cfg, wifi_cache)) {
    CleanupHotPathWifiRuntime(cfg.teardown_policy);
    out.status = HotSendStatus::kWifiFailed;
    out.actual_channel = g_bisect_actual_channel;
    out.negotiated_auth = ReadNegotiatedAuth();
    auto const elapsed = esp_timer_get_time() - t0;
    out.cycle_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
    out.connect_us = out.cycle_us;
    out.fail_stage = 1;
    out.sta_connected_seen =
        g_wifi_sta_connected_seen.load(std::memory_order_relaxed);
    out.got_ip_seen = g_wifi_got_ip_seen.load(std::memory_order_relaxed);
    out.last_disconnect_reason =
        g_wifi_last_disconnect_reason.load(std::memory_order_relaxed);
    {
      auto const d = g_wifi_disconnect_count.load(std::memory_order_relaxed);
      out.disconnect_count = d > 255 ? 255 : static_cast<std::uint8_t>(d);
    }
    return out;
  } else {
    wifi_ready = true;
    t_ready = esp_timer_get_time();
  }

  {
    auto const elapsed = t_ready - t0;
    out.connect_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
  }
  out.wifi_init_us = g_last_wifi_init_us;
  out.heap_before_wifi = g_last_heap_before_wifi;
  out.heap_after_wifi = g_last_heap_after_wifi;
  out.status_flags |=
      static_cast<std::uint8_t>(bench::BisectStatusBits::kWifiReady);
  out.actual_channel = g_bisect_actual_channel;
  out.negotiated_auth = ReadNegotiatedAuth();
  {
    auto const d = g_wifi_disconnect_count.load(std::memory_order_relaxed);
    out.disconnect_count = d > 255 ? 255 : static_cast<std::uint8_t>(d);
  }
  out.last_disconnect_reason =
      g_wifi_last_disconnect_reason.load(std::memory_order_relaxed);
  {
    auto const r = g_wifi_reconnect_count.load(std::memory_order_relaxed);
    out.reconnect_count = r > 255 ? 255 : static_cast<std::uint8_t>(r);
  }

  {
    wifi_ap_record_t ap{};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
      out.rssi = ap.rssi;
      out.ap_primary = ap.primary;
    }
  }

  WaitUntilPreDeadline(t_ready, cfg);
  ApplyPhasePsBeforeTx(cfg);

  HotSendStatus encode_status = HotSendStatus::kWifiFailed;
  auto const t_post0 = esp_timer_get_time();
  if (cfg.post_mode != FastPostMode::kFixedDelay) {
    if (cfg.encode_during_association && artifacts.ready) {
      encode_status =
          SendHotArtifactsWithLateTxDone(artifacts, cfg, &out);
    } else {
      encode_status =
          EncodeAndUdpSendWithLateTxDone(payload, &out, cfg);
    }
    std::uint16_t hold_ms = cfg.post_delay_ms;
    if (cfg.post_mode == FastPostMode::kTxDoneCbPlus10) {
      if (hold_ms < 10) {
        hold_ms = 10;
      }
    } else if (cfg.post_mode == FastPostMode::kTxDoneCbPlus25) {
      if (hold_ms < 25) {
        hold_ms = 25;
      }
    }
    // The hold starts where the TX-done wait ended, so it is the time the radio
    // is deliberately kept up after the frame was acknowledged as transmitted.
    if (hold_ms > 0 && HotSendConsumedNonce(encode_status)) {
      vTaskDelay(pdMS_TO_TICKS(hold_ms));
    }
  } else {
    encode_status = EncodeAndUdpSendTracked(payload);
    auto const t_send_done = esp_timer_get_time();
    {
      auto const es = t_send_done - t_post0;
      out.encode_send_us = es < 0 ? 0 : static_cast<std::uint32_t>(es);
    }
    if (encode_status == HotSendStatus::kSent && cfg.post_delay_ms > 0) {
      vTaskDelay(pdMS_TO_TICKS(cfg.post_delay_ms));
    }
  }

  if (HotSendConsumedNonce(encode_status)) {
    out.sendto_ok = 1;
    out.status_flags |=
        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk) |
        static_cast<std::uint8_t>(bench::BisectStatusBits::kSendtoOk);
  } else if (encode_status == HotSendStatus::kSendFailed) {
    out.fail_stage = 3;
    out.status_flags |=
        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk);
  } else if (encode_status == HotSendStatus::kEncodeFailed) {
    out.fail_stage = 2;
  }

  {
    wifi_ap_record_t ap{};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
      out.rssi = ap.rssi;
      out.ap_primary = ap.primary;
      out.actual_channel = ap.primary != 0 ? ap.primary : out.actual_channel;
      std::memcpy(out.bssid, ap.bssid, sizeof(out.bssid));
    }
  }
  out.sta_connected_seen =
      g_wifi_sta_connected_seen.load(std::memory_order_relaxed);
  out.got_ip_seen = g_wifi_got_ip_seen.load(std::memory_order_relaxed);
  out.used_static_arp = g_last_used_static_arp;
  out.arp_fallback_used = g_last_arp_fallback;
  out.used_cached_channel =
      (cfg.use_channel && cache.channel != 0) ? 1 : 0;
  out.used_static_ip = (cfg.use_static_ip && cache.valid_ip) ? 1 : 0;

  // Refresh live association into g_bisect_cache for subsequent HOT.
  if (g_wifi_netif != nullptr) {
    esp_netif_ip_info_t ip_info{};
    if (esp_netif_get_ip_info(g_wifi_netif, &ip_info) == ESP_OK &&
        ip_info.ip.addr != 0) {
      g_bisect_cache.valid_ip = true;
      g_bisect_cache.ip = ip_info.ip.addr;
      g_bisect_cache.netmask = ip_info.netmask.addr;
      g_bisect_cache.gateway = ip_info.gw.addr;
    }
  }
  if (out.actual_channel != 0) {
    g_bisect_cache.channel = out.actual_channel;
  }
  if (out.bssid[0] | out.bssid[1] | out.bssid[2] | out.bssid[3] |
      out.bssid[4] | out.bssid[5]) {
    g_bisect_cache.valid_bssid = true;
    std::memcpy(g_bisect_cache.bssid, out.bssid, sizeof(out.bssid));
  }
  if (gateway_mac_valid) {
    g_bisect_cache.valid_gw_mac = true;
    std::memcpy(g_bisect_cache.gw_mac, gateway_mac, sizeof(gateway_mac));
  }

  auto const t_teardown0 = esp_timer_get_time();
  // The POST delay as it really happened: from the transmission the driver
  // confirmed to the moment the radio starts coming down.
  if (out.tx_done_confirmed != 0) {
    out.actual_post_us = NonNegativeUs(
        t_teardown0 - static_cast<std::int64_t>(out.first_success_callback_us));
  }
  CleanupHotPathWifiRuntime(cfg.teardown_policy);
  auto const t_end = esp_timer_get_time();
  {
    auto const td = t_end - t_teardown0;
    out.teardown_us = td < 0 ? 0 : static_cast<std::uint32_t>(td);
  }

  auto const elapsed = t_end - t0;
  out.cycle_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
  out.status = encode_status;
  if (encode_status == HotSendStatus::kWifiFailed) {
    out.fail_stage = 1;
  }
  return out;
}

FastSendResult SendPreparedOnceReliability(
    FastPathConfig const& cfg, ae::DataBuffer const& payload,
    BisectWifiCacheSnapshot* wifi_cache) {
  BisectWifiCacheSnapshot local =
      wifi_cache != nullptr ? *wifi_cache : g_bisect_cache;

  struct Attempt {
    bool use_channel;
    bool use_static_ip;
    bool use_static_arp;
    std::uint8_t channel_fb;
    std::uint8_t dhcp_fb;
  };
  Attempt const attempts[] = {
      {cfg.use_channel && local.channel != 0, cfg.use_static_ip && local.valid_ip,
       cfg.use_static_arp && local.valid_gw_mac, 0, 0},
      {false, cfg.use_static_ip && local.valid_ip,
       cfg.use_static_arp && local.valid_gw_mac, 1, 0},
      {false, false, false, 1, 1},
  };

  FastSendResult last{};
  last.fail_stage = 1;
  for (auto const& att : attempts) {
    FastPathConfig c = cfg;
    c.use_bssid = false;
    c.use_channel = att.use_channel;
    c.use_static_ip = att.use_static_ip;
    c.use_static_arp = att.use_static_arp;
    c.arp_wait_on_miss = true;

    last = SendPreparedOnceWithFastPath(c, payload, &local);
    last.channel_fallback_used = att.channel_fb;
    last.dhcp_fallback_used = att.dhcp_fb;
    last.used_cached_channel = att.use_channel ? 1 : 0;
    last.used_static_ip = att.use_static_ip ? 1 : 0;

    if (last.status != HotSendStatus::kWifiFailed) {
      local = g_bisect_cache;
      if (wifi_cache != nullptr) {
        *wifi_cache = local;
      }
      return last;
    }
  }
  if (wifi_cache != nullptr) {
    *wifi_cache = local;
  }
  return last;
}

namespace {
std::uint32_t Crc32Bytes(void const* data, std::size_t len) {
  auto const* p = static_cast<std::uint8_t const*>(data);
  std::uint32_t crc = 0xffffffffu;
  for (std::size_t i = 0; i < len; ++i) {
    crc ^= p[i];
    for (int b = 0; b < 8; ++b) {
      std::uint32_t const mask = -(crc & 1u);
      crc = (crc >> 1) ^ (0xedb88320u & mask);
    }
  }
  return ~crc;
}
}  // namespace

bool PreparedWifiRtcCacheIsValid(PreparedWifiRtcCache const& cache) {
  if (cache.magic != kPreparedWifiRtcMagic ||
      cache.version != kPreparedWifiRtcVersion) {
    return false;
  }
  PreparedWifiRtcCache tmp = cache;
  tmp.crc = 0;
  auto const expect = Crc32Bytes(&tmp, sizeof(tmp));
  if (expect != cache.crc) {
    return false;
  }
  bool const have_ip = (cache.flags & 1u) != 0;
  bool const have_ch = (cache.flags & 2u) != 0;
  bool const have_gw = (cache.flags & 4u) != 0;
  (void)have_gw;
  // Gateway MAC is preferred but optional: hot path can ARP on miss.
  return have_ip && have_ch && cache.channel != 0 && cache.ip != 0;
}

BisectWifiCacheSnapshot SnapshotFromPreparedWifiRtcCache(
    PreparedWifiRtcCache const& cache) {
  BisectWifiCacheSnapshot s{};
  if (!PreparedWifiRtcCacheIsValid(cache)) {
    return s;
  }
  s.valid_ip = true;
  s.valid_gw_mac = (cache.flags & 4u) != 0;
  s.valid_bssid = (cache.flags & 8u) != 0;
  s.channel = cache.channel;
  s.ip = cache.ip;
  s.netmask = cache.netmask;
  s.gateway = cache.gateway;
  std::memcpy(s.gw_mac, cache.gw_mac, sizeof(s.gw_mac));
  std::memcpy(s.bssid, cache.bssid, sizeof(s.bssid));
  return s;
}

bool CapturePreparedWifiRtcCache(PreparedWifiRtcCache* out) {
  if (out == nullptr) {
    return false;
  }
  if (!FreezeBisectWifiCacheFromActiveConnection()) {
    // Fallback: production-style capture then freeze again.
    if (!CapturePreparedWifiCacheFromActiveConnection()) {
      return false;
    }
    if (!FreezeBisectWifiCacheFromActiveConnection()) {
      return false;
    }
  }
  // Some APs report primary=0 briefly; HOT path needs a non-zero channel.
  if (g_bisect_cache.channel == 0) {
    wifi_ap_record_t ap{};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK && ap.primary != 0) {
      g_bisect_cache.channel = ap.primary;
    } else {
      g_bisect_cache.channel = 1;
    }
  }
  PreparedWifiRtcCache c{};
  c.magic = kPreparedWifiRtcMagic;
  c.version = kPreparedWifiRtcVersion;
  c.flags = 0;
  if (g_bisect_cache.valid_ip) {
    c.flags |= 1u;
    c.ip = g_bisect_cache.ip;
    c.netmask = g_bisect_cache.netmask;
    c.gateway = g_bisect_cache.gateway;
  }
  if (g_bisect_cache.channel != 0) {
    c.flags |= 2u;
    c.channel = g_bisect_cache.channel;
  }
  if (g_bisect_cache.valid_gw_mac) {
    c.flags |= 4u;
    std::memcpy(c.gw_mac, g_bisect_cache.gw_mac, sizeof(c.gw_mac));
  }
  if (g_bisect_cache.valid_bssid) {
    c.flags |= 8u;
    std::memcpy(c.bssid, g_bisect_cache.bssid, sizeof(c.bssid));
  }
  c.crc = 0;
  c.crc = Crc32Bytes(&c, sizeof(c));
  *out = c;
  return PreparedWifiRtcCacheIsValid(*out);
}

FastPathConfig FastPathConfigForProbeProfile(ae::WifiProbeProfile profile,
                                             std::uint16_t pre_ms,
                                             std::uint16_t post_ms) {
  FastPathConfig c{};
  c.use_bssid = false;
  c.use_channel = ae::WifiProbeProfileUsesChannel(profile);
  c.use_static_ip = ae::WifiProbeProfileUsesCachedIp(profile);
  c.use_static_arp = ae::WifiProbeProfileUsesArp(profile);
  c.pre_delay_ms = pre_ms;
  c.post_delay_ms = post_ms;
  c.post_mode = FastPostMode::kFixedDelay;
  return c;
}

void ApplyProbeStateToHotConfig(ae::WifiProbeRtcState const& state,
                                FastPathConfig* cfg,
                                BisectWifiCacheSnapshot* cache) {
  if (cfg == nullptr) {
    return;
  }
  auto const profile =
      static_cast<ae::WifiProbeProfile>(state.selected_profile);
  *cfg = FastPathConfigForProbeProfile(profile, state.pre_send_delay_ms,
                                       state.post_send_delay_ms);
  if (cache == nullptr) {
    return;
  }
  cache->valid_ip = state.ip != 0;
  cache->valid_gw_mac = false;
  for (unsigned i = 0; i < 6; ++i) {
    if (state.gateway_mac[i] != 0) {
      cache->valid_gw_mac = true;
      break;
    }
  }
  cache->valid_bssid = false;
  cache->channel = state.channel;
  cache->ip = state.ip;
  cache->netmask = state.netmask;
  cache->gateway = state.gateway;
  std::memcpy(cache->gw_mac, state.gateway_mac, sizeof(cache->gw_mac));
  std::memcpy(cache->bssid, state.bssid, sizeof(cache->bssid));
}

void ApplyPowerBenchToFastPath(FastPathConfig* cfg,
                               std::uint8_t teardown_policy, bool pmf_off,
                               std::uint8_t connected_ps_mode,
                               std::uint8_t listen_interval,
                               std::uint8_t phase_ps,
                               bool encode_during_association) {
  if (cfg == nullptr) {
    return;
  }
  cfg->pmf_off = pmf_off;
  cfg->teardown_policy = teardown_policy;
  cfg->listen_interval = listen_interval != 0 ? listen_interval : 1;
  cfg->phase_ps = phase_ps;
  cfg->encode_during_association = encode_during_association;
  std::uint8_t ps = connected_ps_mode;
  if (phase_ps == 1 && ps == 0) {
    ps = 1;
  }
  if (phase_ps == 2 && ps == 0) {
    ps = 2;
  }
  if (phase_ps == 3) {
    ps = 1;
  }
  if (phase_ps == 4) {
    ps = 2;
  }
  cfg->connected_ps_mode = ps;
}

void RecordHotProbeFailure(ae::WifiProbeRtcState* state,
                           ae::WifiProbeRecoveryReason reason) {
  if (state == nullptr) {
    return;
  }
  ae::WifiProbeDegradeSelected(*state, reason);
}
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
