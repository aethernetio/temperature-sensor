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
  g_tx_diag.total.fetch_add(1, std::memory_order_relaxed);
  g_fast_cb_count.fetch_add(1, std::memory_order_relaxed);
  g_tx_diag.last_cb_us.store(now, std::memory_order_relaxed);

  int expected_first = -1;
  if (g_tx_diag.first_status.compare_exchange_strong(
          expected_first, txStatus ? 1 : 0, std::memory_order_relaxed)) {
    g_tx_diag.first_cb_us.store(now, std::memory_order_relaxed);
  }

  if (txStatus) {
    g_tx_diag.success.fetch_add(1, std::memory_order_relaxed);
    std::int64_t expected_fs = 0;
    if (g_tx_diag.first_success_us.compare_exchange_strong(
            expected_fs, now, std::memory_order_relaxed)) {
      g_fast_tx_done_success.store(true, std::memory_order_release);
    }
  } else {
    g_tx_diag.failed.fetch_add(1, std::memory_order_relaxed);
    std::int64_t expected_ff = 0;
    (void)g_tx_diag.first_failed_us.compare_exchange_strong(
        expected_ff, now, std::memory_order_relaxed);
  }

  g_fast_tx_done_seen.store(true, std::memory_order_release);
}

void ResetFastTxDone() { ResetTxDoneDiag(); }

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
HotSendStatus EncodeAndUdpSendWithLateTxDone(ae::DataBuffer const& payload,
                                             FastSendResult* timing,
                                             FastTxDoneWaitMode wait_mode) {
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

  ResetFastTxDone();
  g_tx_wait_mode.store(static_cast<int>(wait_mode), std::memory_order_relaxed);
  (void)esp_wifi_set_tx_done_cb(&FastTxDoneCb);

  auto sent = sendto(sock, packet.data(), packet.size(), 0,
                     reinterpret_cast<sockaddr*>(&dest_storage), dest_len);
  auto const t_send_ret = esp_timer_get_time();
  if (timing != nullptr) {
    auto const es = t_send_ret - t_encode0;
    timing->encode_send_us =
        es < 0 ? 0 : static_cast<std::uint32_t>(es);
  }

  if (sent != static_cast<ssize_t>(packet.size())) {
    (void)esp_wifi_set_tx_done_cb(nullptr);
    close(sock);
    return HotSendStatus::kSendFailed;
  }

  constexpr std::int64_t kMaxUs = 100000;
  constexpr std::int64_t kObserveUs = 5000;
  bool condition_met = false;
  std::uint32_t total_at_success = 0;

  auto condition_ready = [&]() -> bool {
    if (wait_mode == FastTxDoneWaitMode::kFirstSuccess) {
      return g_fast_tx_done_success.load(std::memory_order_acquire);
    }
    return g_fast_tx_done_seen.load(std::memory_order_acquire);
  };

  while ((esp_timer_get_time() - t_send_ret) < kMaxUs) {
    if (condition_ready()) {
      condition_met = true;
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(1));
  }

  if (condition_met && wait_mode == FastTxDoneWaitMode::kFirstSuccess) {
    total_at_success = g_tx_diag.total.load(std::memory_order_relaxed);
    auto const t_obs0 = esp_timer_get_time();
    while ((esp_timer_get_time() - t_obs0) < kObserveUs) {
      vTaskDelay(pdMS_TO_TICKS(1));
    }
  }

  auto const t_cb_done = esp_timer_get_time();
  (void)esp_wifi_set_tx_done_cb(nullptr);
  close(sock);

  std::uint8_t after_success = 0;
  if (condition_met && wait_mode == FastTxDoneWaitMode::kFirstSuccess) {
    auto const total_end = g_tx_diag.total.load(std::memory_order_relaxed);
    auto const delta =
        total_end > total_at_success ? (total_end - total_at_success) : 0u;
    after_success = delta > 255 ? 255 : static_cast<std::uint8_t>(delta);
  }

  if (timing != nullptr) {
    auto const wait = t_cb_done - t_send_ret;
    timing->tx_done_wait_us =
        wait < 0 ? 0 : static_cast<std::uint32_t>(wait);
    FillTxDoneTiming(timing, t_send_ret, condition_met, after_success,
                     wait_mode);
  }
  return HotSendStatus::kSent;
}
#endif

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

std::uint8_t ReadNegotiatedAuth() {
  wifi_ap_record_t ap_info{};
  if (esp_wifi_sta_get_ap_info(&ap_info) != ESP_OK) {
    return 0;
  }
  return static_cast<std::uint8_t>(ap_info.authmode);
}

bool StartFastWifi(FastPathConfig const& cfg,
                   BisectWifiCacheSnapshot const* cache_override) {
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

  bool const need_static_ip = cfg.use_static_ip && cache.valid_ip;
  g_wait_got_ip = !need_static_ip;
  g_using_bssid_cache = cfg.use_bssid && cache.valid_bssid;
  g_max_wifi_retry = cfg.retry_max;

  wifi_init_config_t wifi_init_cfg = WIFI_INIT_CONFIG_DEFAULT();
  if (cfg.ampdu_tx_off) {
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
        .ip = {.addr = cache.ip},
        .netmask = {.addr = cache.netmask},
        .gw = {.addr = cache.gateway}};
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

  wifi_config.sta.pmf_cfg.capable = true;
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
  (void)esp_wifi_set_ps(WIFI_PS_NONE);
  // TX-done callback is installed immediately before sendto() for callback
  // post modes — never here (association / PRE would fire unrelated TX).

  EventBits_t bits = xEventGroupWaitBits(
      g_wifi_event_group, kWifiReadyBit | kWifiFailBit, pdFALSE, pdFALSE,
      pdMS_TO_TICKS(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS));

  if ((bits & kWifiReadyBit) == 0) {
    return false;
  }

  g_bisect_actual_channel = ReadActualChannel();

  if (cfg.use_static_arp && cache.valid_gw_mac && cache.valid_ip) {
    std::memcpy(gateway_mac, cache.gw_mac, sizeof(gateway_mac));
    gateway_mac_valid = true;
    (void)InstallStaticGatewayArp();
  }

  return true;
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
  if (!StartFastWifi(cfg, wifi_cache)) {
    CleanupHotPathWifiRuntime();
    out.status = HotSendStatus::kWifiFailed;
    out.actual_channel = g_bisect_actual_channel;
    out.negotiated_auth = ReadNegotiatedAuth();
    auto const elapsed = esp_timer_get_time() - t0;
    out.cycle_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
    out.connect_us = out.cycle_us;
    return out;
  }

  auto const t_ready = esp_timer_get_time();
  {
    auto const elapsed = t_ready - t0;
    out.connect_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
  }
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

  if (cfg.pre_delay_ms > 0) {
    vTaskDelay(pdMS_TO_TICKS(cfg.pre_delay_ms));
  }

  HotSendStatus encode_status = HotSendStatus::kWifiFailed;
  auto const t_post0 = esp_timer_get_time();
  if (cfg.post_mode != FastPostMode::kFixedDelay) {
    encode_status =
        EncodeAndUdpSendWithLateTxDone(payload, &out, cfg.tx_done_wait);
    std::uint16_t extra_ms = 0;
    if (cfg.post_mode == FastPostMode::kTxDoneCbPlus10) {
      extra_ms = 10;
    } else if (cfg.post_mode == FastPostMode::kTxDoneCbPlus25) {
      extra_ms = 25;
    }
    if (extra_ms > 0) {
      vTaskDelay(pdMS_TO_TICKS(extra_ms));
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

  if (encode_status == HotSendStatus::kSent) {
    out.status_flags |=
        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk) |
        static_cast<std::uint8_t>(bench::BisectStatusBits::kSendtoOk);
  } else if (encode_status == HotSendStatus::kSendFailed) {
    out.status_flags |=
        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk);
  } else if (encode_status == HotSendStatus::kEncodeFailed) {
    // encode failed: no sendto bit
  }

  auto const t_teardown0 = esp_timer_get_time();
  CleanupHotPathWifiRuntime();
  auto const t_end = esp_timer_get_time();
  {
    auto const td = t_end - t_teardown0;
    out.teardown_us = td < 0 ? 0 : static_cast<std::uint32_t>(td);
  }

  auto const elapsed = t_end - t0;
  out.cycle_us = elapsed < 0 ? 0 : static_cast<std::uint32_t>(elapsed);
  out.status = encode_status;
  return out;
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
