/*
 * Cached Wi-Fi + raw lwIP UDP (from udp_rtc_index_loop @ 8f7b8d5293c132b6512318170286eaaff9f96a84).
 */
#include "udp_cached_lwip_send.h"

#if defined(ESP_PLATFORM)

#include <cstring>

#include <esp_event.h>
#include <esp_netif.h>
#include <esp_netif_net_stack.h>
#include <esp_private/wifi.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <lwip/etharp.h>
#include <lwip/ip_addr.h>
#include <lwip/netif.h>
#include <lwip/pbuf.h>
#include <lwip/udp.h>
#include <nvs_flash.h>

extern "C" esp_err_t esp_wifi_internal_set_retry_counter(uint8_t short_retry,
                                                         uint8_t long_retry);

namespace temp_sensor::udp_cached_lwip {
namespace {

constexpr int kGotIpBit = BIT0;
#if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
#  ifndef AE_UDP_PRE_SETTLE_MS
#    define AE_UDP_PRE_SETTLE_MS 50
#  endif
#  ifndef AE_UDP_POST_SEND_HOLD_MS
#    define AE_UDP_POST_SEND_HOLD_MS 200
#  endif
constexpr int kPreSettleMs = AE_UDP_PRE_SETTLE_MS;
constexpr int kPostSendHoldMs = AE_UDP_POST_SEND_HOLD_MS;
#  ifndef AE_UDP_MEASURED_SENDS
#    define AE_UDP_MEASURED_SENDS 100
#  endif
#  ifndef AE_UDP_PERIOD_S
#    define AE_UDP_PERIOD_S 60
#  endif
constexpr std::uint32_t kMeasuredSends = AE_UDP_MEASURED_SENDS;
constexpr std::uint64_t kPeriodUs =
    static_cast<std::uint64_t>(AE_UDP_PERIOD_S) * 1000000ULL;
constexpr std::uint64_t kWarmupSleepUs = 8ULL * 1000000ULL;
constexpr std::uint64_t kDoneSleepUs = 60ULL * 60ULL * 1000000ULL;
constexpr int kColdBootFlashWindowS = 60;
#else
constexpr int kPreSettleMs = 50;
constexpr int kPostSendHoldMs = 200;
constexpr std::uint64_t kSleepUs = 10ULL * 1000000ULL;
#endif
constexpr int kArpResolveTimeoutMs = 500;
constexpr int kArpLearnTimeoutMs = 1500;  // one-shot learn during post-send hold
constexpr TickType_t kGotIpTimeoutTicks = pdMS_TO_TICKS(10000);

// --- Wi-Fi RTC caches (channel / BSSID / static IP / ARP). Default ON. ---
// AE_UDP_WIFI_CACHE=0 disables all five for no-cache comparison runs.
#  ifndef AE_UDP_WIFI_CACHE
#    define AE_UDP_WIFI_CACHE 1
#  endif
constexpr bool kCacheChannel = (AE_UDP_WIFI_CACHE != 0);    // step 1
constexpr bool kCacheStaticIp = (AE_UDP_WIFI_CACHE != 0);   // step 2
constexpr bool kCacheBssid = (AE_UDP_WIFI_CACHE != 0);      // step 3
constexpr bool kCachePeerArp = (AE_UDP_WIFI_CACHE != 0);    // step 4
constexpr bool kCacheGwArp = (AE_UDP_WIFI_CACHE != 0);      // step 5

RTC_DATA_ATTR std::uint32_t g_index = 0;
RTC_DATA_ATTR std::uint8_t g_have_channel = 0;
RTC_DATA_ATTR std::uint8_t g_have_static_ip = 0;
RTC_DATA_ATTR std::uint8_t g_have_bssid = 0;
RTC_DATA_ATTR std::uint8_t g_have_peer_mac = 0;
RTC_DATA_ATTR std::uint8_t g_have_gw_mac = 0;

RTC_DATA_ATTR std::uint8_t g_channel = 0;
RTC_DATA_ATTR std::uint8_t g_bssid[6] = {};
RTC_DATA_ATTR std::uint32_t g_ip = 0;
RTC_DATA_ATTR std::uint32_t g_netmask = 0;
RTC_DATA_ATTR std::uint32_t g_gateway = 0;
RTC_DATA_ATTR std::uint32_t g_peer_ip = 0;
RTC_DATA_ATTR std::uint8_t g_peer_mac[6] = {};
RTC_DATA_ATTR std::uint8_t g_gw_mac[6] = {};

#if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
RTC_DATA_ATTR std::uint64_t g_next_deadline_us = 0;
RTC_DATA_ATTR std::uint8_t g_have_deadline = 0;
RTC_DATA_ATTR std::uint32_t g_overrun = 0;
#endif

EventGroupHandle_t g_events = nullptr;
// Reconnect only until GOT_IP. After that, a mid-hold disconnect + auto
// reconnect makes esp_wifi_stop() block on the in-flight connect (~15тАУ20s).
bool g_want_reconnect = false;

void OnWifiEvent(void*, esp_event_base_t base, std::int32_t id, void*) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    if (g_want_reconnect) {
      esp_wifi_connect();
    }
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    if (g_want_reconnect) {
      esp_wifi_connect();
    }
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    xEventGroupSetBits(g_events, kGotIpBit);
  }
}

bool MacIsUnicast(const std::uint8_t mac[6]) {
  if ((mac[0] & 0x01u) != 0) {
    return false;  // multicast / broadcast
  }
  for (int i = 0; i < 6; ++i) {
    if (mac[i] != 0) {
      return true;
    }
  }
  return false;
}

// Minimal ARP plumbing (only used when kCachePeerArp / kCacheGwArp).
struct ArpCtx {
  struct netif* lwip_netif{nullptr};
  ip4_addr_t ip{};
  std::uint8_t mac[6]{};
  bool found{false};
  struct eth_addr eth{};
  err_t err{ERR_VAL};
};

esp_err_t ArpLookupTcpip(void* arg) {
  auto* c = static_cast<ArpCtx*>(arg);
  struct eth_addr* eth_ret = nullptr;
  const ip4_addr_t* ip_ret = nullptr;
  if (etharp_find_addr(c->lwip_netif, &c->ip, &eth_ret, &ip_ret) >= 0 &&
      eth_ret != nullptr) {
    std::memcpy(c->mac, eth_ret->addr, sizeof(c->mac));
    c->found = true;
  }
  return ESP_OK;
}

esp_err_t ArpRequestTcpip(void* arg) {
  auto* c = static_cast<ArpCtx*>(arg);
  (void)etharp_request(c->lwip_netif, &c->ip);
  return ESP_OK;
}

esp_err_t ArpAddStaticTcpip(void* arg) {
  auto* c = static_cast<ArpCtx*>(arg);
  c->err = etharp_add_static_entry(&c->ip, &c->eth);
  return ESP_OK;
}

bool ResolveMac(esp_netif_t* netif, std::uint32_t ip, std::uint8_t out_mac[6],
                int timeout_ms = kArpResolveTimeoutMs) {
  auto* lwip_netif =
      static_cast<struct netif*>(esp_netif_get_netif_impl(netif));
  if (lwip_netif == nullptr || ip == 0) {
    return false;
  }
  ArpCtx c{};
  c.lwip_netif = lwip_netif;
  c.ip.addr = ip;
  if (esp_netif_tcpip_exec(&ArpLookupTcpip, &c) == ESP_OK && c.found &&
      MacIsUnicast(c.mac)) {
    std::memcpy(out_mac, c.mac, 6);
    return true;
  }
  (void)esp_netif_tcpip_exec(&ArpRequestTcpip, &c);
  TickType_t const deadline =
      xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  while (xTaskGetTickCount() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(20));
    c.found = false;
    if (esp_netif_tcpip_exec(&ArpLookupTcpip, &c) == ESP_OK && c.found &&
        MacIsUnicast(c.mac)) {
      std::memcpy(out_mac, c.mac, 6);
      return true;
    }
  }
  return false;
}

bool InstallStaticArp(std::uint32_t ip, const std::uint8_t mac[6]) {
  if (ip == 0 || !MacIsUnicast(mac)) {
    return false;
  }
  ArpCtx c{};
  c.ip.addr = ip;
  std::memcpy(c.eth.addr, mac, 6);
  if (esp_netif_tcpip_exec(&ArpAddStaticTcpip, &c) != ESP_OK) {
    return false;
  }
  return c.err == ERR_OK;
}

esp_err_t ArpRemoveStaticTcpip(void* arg) {
  auto* c = static_cast<ArpCtx*>(arg);
  c->err = etharp_remove_static_entry(&c->ip);
  return ESP_OK;
}

void RemoveStaticArp(std::uint32_t ip) {
  if (ip == 0) {
    return;
  }
  ArpCtx c{};
  c.ip.addr = ip;
  (void)esp_netif_tcpip_exec(&ArpRemoveStaticTcpip, &c);
}

esp_err_t ArpGratuitousTcpip(void* arg) {
  auto* lwip_netif = static_cast<struct netif*>(arg);
  if (lwip_netif != nullptr) {
    etharp_gratuitous(lwip_netif);
  }
  return ESP_OK;
}

// One-shot announce (not the periodic CONFIG_LWIP_ESP_GRATUITOUS_ARP timer).
void SendOneShotGar(esp_netif_t* netif) {
  auto* lwip_netif =
      static_cast<struct netif*>(esp_netif_get_netif_impl(netif));
  if (lwip_netif != nullptr) {
    (void)esp_netif_tcpip_exec(&ArpGratuitousTcpip, lwip_netif);
  }
}

// Raw lwIP UDP send (must run on TCP/IP thread via esp_netif_tcpip_exec).
struct UdpSendCtx {
  ip_addr_t dest_ip{};
  std::uint16_t port{0};
  std::uint8_t const* payload_ptr{nullptr};
  std::size_t payload_len{0};;
  err_t err{ERR_VAL};
};

esp_err_t UdpSendTcpip(void* arg) {
  auto* c = static_cast<UdpSendCtx*>(arg);
  struct udp_pcb* upcb = udp_new();
  if (upcb == nullptr) {
    c->err = ERR_MEM;
    return ESP_OK;
  }
  struct pbuf* p =
      pbuf_alloc(PBUF_TRANSPORT, static_cast<u16_t>(c->payload_len), PBUF_RAM);
  if (p == nullptr) {
    udp_remove(upcb);
    c->err = ERR_MEM;
    return ESP_OK;
  }
  if (c->payload_len > 0 && c->payload_ptr != nullptr) {
    std::memcpy(p->payload, c->payload_ptr, c->payload_len);
  }
  c->err = udp_sendto(upcb, p, &c->dest_ip, c->port);
  pbuf_free(p);
  udp_remove(upcb);
  return ESP_OK;
}

bool WaitGotIpBounded() {
  EventBits_t bits =
      xEventGroupWaitBits(g_events, kGotIpBit, pdFALSE, pdFALSE,
                          kGotIpTimeoutTicks);
  if ((bits & kGotIpBit) != 0) {
    return true;
  }
  // Soft reconnect already runs via STA_DISCONNECTED; nudge once more.
  xEventGroupClearBits(g_events, kGotIpBit);
  (void)esp_wifi_disconnect();
  vTaskDelay(pdMS_TO_TICKS(50));
  (void)esp_wifi_connect();
  bits = xEventGroupWaitBits(g_events, kGotIpBit, pdFALSE, pdFALSE,
                             kGotIpTimeoutTicks);
  return (bits & kGotIpBit) != 0;
}

}  // anonymous namespace

SendResult SendOnce(char const* ssid, char const* password,
                    prepared_send::PreparedWifiRtcCache const& cache,
                    SendRequest const& req) {
  SendResult out{};
  if (ssid == nullptr || password == nullptr || req.payload == nullptr ||
      req.payload_len == 0 || req.dest_ipv4 == 0 || req.dest_port == 0 ||
      !prepared_send::PreparedWifiRtcCacheIsValid(cache)) {
    return out;
  }
  const int hold_ms = req.post_send_hold_ms < 200 ? 200 : req.post_send_hold_ms;

  g_want_reconnect = true;
  (void)nvs_flash_init();
  (void)esp_netif_init();
  (void)esp_event_loop_create_default();
  esp_netif_t* netif = esp_netif_create_default_wifi_sta();
  g_events = xEventGroupCreate();

  wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
  (void)esp_wifi_init(&wcfg);
  (void)esp_wifi_set_storage(WIFI_STORAGE_RAM);
  (void)esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnWifiEvent, nullptr);
  (void)esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &OnWifiEvent, nullptr);

  if (cache.ip != 0) {
    (void)esp_netif_dhcpc_stop(netif);
    esp_netif_ip_info_t ip_info = {.ip = {.addr = cache.ip},
                                   .netmask = {.addr = cache.netmask},
                                   .gw = {.addr = cache.gateway}};
    (void)esp_netif_set_ip_info(netif, &ip_info);
  }

  wifi_config_t wifi{};
  std::strncpy(reinterpret_cast<char*>(wifi.sta.ssid), ssid, sizeof(wifi.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wifi.sta.password), password, sizeof(wifi.sta.password));
  wifi.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  wifi.sta.scan_method = WIFI_FAST_SCAN;
  if (cache.channel != 0) {
    wifi.sta.channel = cache.channel;
  }
  if ((cache.flags & 8u) != 0) {
    wifi.sta.bssid_set = true;
    std::memcpy(wifi.sta.bssid, cache.bssid, sizeof(wifi.sta.bssid));
  }

  (void)esp_wifi_set_mode(WIFI_MODE_STA);
  (void)esp_wifi_set_config(WIFI_IF_STA, &wifi);
  (void)esp_wifi_set_protocol(WIFI_IF_STA,
                              WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
  (void)esp_wifi_start();
  (void)esp_wifi_set_ps(WIFI_PS_NONE);

  out.got_ip = WaitGotIpBounded();
  g_want_reconnect = false;

  if (out.got_ip) {
    (void)esp_wifi_set_ps(WIFI_PS_MAX_MODEM);
    if (req.use_peer_static_arp) {
      (void)InstallStaticArp(req.dest_ipv4, cache.gw_mac);
    } else if ((cache.flags & 4u) != 0 && cache.gateway != 0) {
      (void)InstallStaticArp(cache.gateway, cache.gw_mac);
    }
    SendOneShotGar(netif);
    vTaskDelay(pdMS_TO_TICKS(kPreSettleMs));
    (void)esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true, WIFI_PHY_RATE_1M_L);
    (void)esp_wifi_internal_set_retry_counter(3, 3);

    UdpSendCtx send{};
    ip_addr_set_ip4_u32(&send.dest_ip, req.dest_ipv4);
    send.port = req.dest_port;
    send.payload_ptr = req.payload;
    send.payload_len = req.payload_len;
    (void)esp_netif_tcpip_exec(&UdpSendTcpip, &send);
    out.lwip_err = static_cast<int>(send.err);
    out.udp_send_ok = (send.err == ERR_OK);

    TickType_t const hold_deadline = xTaskGetTickCount() + pdMS_TO_TICKS(hold_ms);
    TickType_t const now = xTaskGetTickCount();
    if (now < hold_deadline) {
      vTaskDelay(hold_deadline - now);
    }

    if (req.use_peer_static_arp) {
      RemoveStaticArp(req.dest_ipv4);
    } else if ((cache.flags & 4u) != 0 && cache.gateway != 0) {
      RemoveStaticArp(cache.gateway);
    }
  }

  (void)esp_event_handler_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnWifiEvent);
  (void)esp_event_handler_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, &OnWifiEvent);
  (void)esp_wifi_stop();
  (void)esp_wifi_deinit();
  if (netif != nullptr) {
    esp_netif_destroy_default_wifi(netif);
  }
  if (g_events != nullptr) {
    vEventGroupDelete(g_events);
    g_events = nullptr;
  }
  (void)esp_event_loop_delete_default();
  (void)esp_netif_deinit();
  return out;
}

}  // namespace temp_sensor::udp_cached_lwip

#endif  // ESP_PLATFORM
