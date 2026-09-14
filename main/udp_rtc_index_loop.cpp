/*
 * Copyright 2026 Aethernet Inc.
 *
 * UDP RTC index bisect (AETHER_DIAG_UDP_RTC_INDEX).
 *
 * Fixed across steps:
 *   wait GOT_IP → PS_MAX_MODEM → 50 ms settle → udp_sendto → 200 ms hold →
 *   full teardown → deep sleep 10 s
 *   PS_NONE through association/GOT_IP; AMPDU defaults; Wi-Fi 4 + fixed 1M
 *
 * Toggle ONE cache flag per flash (see kCache* below).
 */

#include "udp_rtc_index_loop.h"

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_UDP_RTC_INDEX)

#  include <cstdint>
#  include <cstring>

#  include <freertos/FreeRTOS.h>
#  include <freertos/event_groups.h>
#  include <freertos/task.h>

#  include <driver/gpio.h>
#  include <esp_attr.h>
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_netif_net_stack.h>
#  include <esp_private/wifi.h>
#  include <esp_sleep.h>
#  include <esp_wifi.h>
#  include <nvs_flash.h>
#  include <sdkconfig.h>

#  if !CONFIG_ULP_COPROC_ENABLED
#    error "AETHER_DIAG_UDP_RTC_INDEX requires CONFIG_ULP_COPROC_ENABLED for ulp_lp_core_stop()"
#  endif
#  include <ulp_lp_core.h>

#  include <lwip/etharp.h>
#  include <lwip/ip_addr.h>
#  include <lwip/netif.h>
#  include <lwip/pbuf.h>
#  include <lwip/udp.h>

// BOARD_AETHER_ESP32_C6 pins from aethernetio/temperature-sensor @ ef5da3b
// (main/boards/aether_esp32_c6.h). UDP diag does not link aether/user_config.
#  include "boards/aether_esp32_c6.h"

// LED_OFF_LEVEL_UNVERIFIED: no schematic/driver in temperature-sensor sets
// STATUS_LED_ON_PIN off polarity; only the pin name exists. Candidate OFF=0
// (PWR_ON is confirmed active-high in stcc4.c). Do not treat as fact.
#  ifndef STATUS_LED_ON_OFF_LEVEL
#    define STATUS_LED_ON_OFF_LEVEL 0
#    define LED_OFF_LEVEL_UNVERIFIED 1
#  endif


extern "C" esp_err_t esp_wifi_internal_set_retry_counter(uint8_t short_retry,
                                                         uint8_t long_retry);

#  ifndef WIFI_SSID
#    error "WIFI_SSID required"
#  endif
#  ifndef WIFI_PASSWORD
#    error "WIFI_PASSWORD required"
#  endif
#  ifndef AE_UDP_SERVER_HOST
#    error "AE_UDP_SERVER_HOST required"
#  endif
#  ifndef AE_UDP_SERVER_PORT
#    define AE_UDP_SERVER_PORT 9000
#  endif

namespace {

constexpr int kGotIpBit = BIT0;
constexpr std::uint64_t kSleepUs = 10ULL * 1000000ULL;
constexpr int kArpResolveTimeoutMs = 500;
constexpr int kArpLearnTimeoutMs = 1500;  // one-shot learn during post-send hold
constexpr TickType_t kGotIpTimeoutTicks = pdMS_TO_TICKS(10000);

// --- Bisect: enable caches one-by-one (exactly one new true per flash) ---
constexpr bool kCacheChannel = true;    // step 1
constexpr bool kCacheStaticIp = true;   // step 2: ip + netmask + gw (still wait GOT_IP)
constexpr bool kCacheBssid = true;      // step 3: pin AP BSSID
constexpr bool kCachePeerArp = true;    // step 4: static ARP for AE_UDP_SERVER_HOST
constexpr bool kCacheGwArp = true;      // step 5: static ARP for gateway

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

EventGroupHandle_t g_events = nullptr;
// Reconnect only until GOT_IP. After that, a mid-hold disconnect + auto
// reconnect makes esp_wifi_stop() block on the in-flight connect (~15–20s).
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
  std::uint8_t payload[4]{};
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
      pbuf_alloc(PBUF_TRANSPORT, sizeof(c->payload), PBUF_RAM);
  if (p == nullptr) {
    udp_remove(upcb);
    c->err = ERR_MEM;
    return ESP_OK;
  }
  std::memcpy(p->payload, c->payload, sizeof(c->payload));
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

// One peripheral power-down for every deep-sleep entry (incl. !got_ip).
// Sensors unused in UDP diag: never enable PWR_ON, never init I2C drivers.
void PeripheralPowerDownForDeepSleep() {
  // Stop LP core if a prior image left it running. Clears LP-timer wakeups and
  // requests sleep (IDF ulp_lp_core_stop). Do not load/run ULP in this path.
  ulp_lp_core_stop();
  (void)esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ULP);

  // SDA/SCL: high-Z, no internal pulls (avoid MCU-side parasitic feed).
  // External pull topology unknown without schematic. Do not hold these
  // (not power-control pins).
  (void)gpio_hold_dis(SENSOR_SDA_PIN);
  (void)gpio_hold_dis(SENSOR_SCL_PIN);
  (void)gpio_reset_pin(SENSOR_SDA_PIN);
  (void)gpio_reset_pin(SENSOR_SCL_PIN);
  (void)gpio_set_direction(SENSOR_SDA_PIN, GPIO_MODE_INPUT);
  (void)gpio_set_direction(SENSOR_SCL_PIN, GPIO_MODE_INPUT);
  (void)gpio_pullup_dis(SENSOR_SDA_PIN);
  (void)gpio_pullup_dis(SENSOR_SCL_PIN);
  (void)gpio_pulldown_dis(SENSOR_SDA_PIN);
  (void)gpio_pulldown_dis(SENSOR_SCL_PIN);

#if BOARD_HAS_PWR_ON == 1
  // stcc4 Init() drives PWR_ON HIGH to enable rail → OFF is LOW.
  {
    esp_err_t err;
    err = gpio_hold_dis(static_cast<gpio_num_t>(PWR_ON_GPIO));
    (void)err;
    (void)gpio_reset_pin(static_cast<gpio_num_t>(PWR_ON_GPIO));
    (void)gpio_set_direction(static_cast<gpio_num_t>(PWR_ON_GPIO),
                             GPIO_MODE_OUTPUT);
    (void)gpio_set_level(static_cast<gpio_num_t>(PWR_ON_GPIO), 0);
    err = gpio_hold_en(static_cast<gpio_num_t>(PWR_ON_GPIO));
    (void)err;
  }
#endif

#if defined(STATUS_LED_ON_PIN)
  // Cut LED rail. OFF level unverified (LED_OFF_LEVEL_UNVERIFIED).
  {
    esp_err_t err;
    err = gpio_hold_dis(STATUS_LED_ON_PIN);
    (void)err;
    (void)gpio_reset_pin(STATUS_LED_ON_PIN);
    (void)gpio_set_direction(STATUS_LED_ON_PIN, GPIO_MODE_OUTPUT);
    (void)gpio_set_level(STATUS_LED_ON_PIN, STATUS_LED_ON_OFF_LEVEL);
    err = gpio_hold_en(STATUS_LED_ON_PIN);
    (void)err;
  }
#endif

#if defined(STATUS_LED_PIN)
  // Do not drive data into a powered-off LED.
  (void)gpio_hold_dis(STATUS_LED_PIN);
  (void)gpio_reset_pin(STATUS_LED_PIN);
  (void)gpio_set_direction(STATUS_LED_PIN, GPIO_MODE_INPUT);
  (void)gpio_pullup_dis(STATUS_LED_PIN);
  (void)gpio_pulldown_dis(STATUS_LED_PIN);
#endif
}

}  // namespace

extern "C" void RunUdpRtcIndexLoop() {
  for (;;) {
    // ------------------------------------------------------------------
    // One wake cycle: keep rails off → caches → GOT_IP → settle → send → sleep
    // ------------------------------------------------------------------
    // Reassert OFF after wake without enabling peripherals.
    PeripheralPowerDownForDeepSleep();
    g_want_reconnect = true;
    nvs_flash_init();
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_t* netif = esp_netif_create_default_wifi_sta();
    g_events = xEventGroupCreate();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    esp_wifi_set_storage(WIFI_STORAGE_RAM);
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnWifiEvent,
                               nullptr);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &OnWifiEvent,
                               nullptr);

    // Cache: static IP (DHCP off + ip/netmask/gw)
    if (kCacheStaticIp && g_have_static_ip) {
      esp_netif_dhcpc_stop(netif);
      esp_netif_ip_info_t ip_info = {.ip = {.addr = g_ip},
                                     .netmask = {.addr = g_netmask},
                                     .gw = {.addr = g_gateway}};
      esp_netif_set_ip_info(netif, &ip_info);
    }

    wifi_config_t wifi{};
    std::strncpy(reinterpret_cast<char*>(wifi.sta.ssid), WIFI_SSID,
                 sizeof(wifi.sta.ssid));
    std::strncpy(reinterpret_cast<char*>(wifi.sta.password), WIFI_PASSWORD,
                 sizeof(wifi.sta.password));
    wifi.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi.sta.scan_method = WIFI_FAST_SCAN;

    // Cache: channel (without BSSID unless kCacheBssid)
    if (kCacheChannel && g_have_channel) {
      wifi.sta.channel = g_channel;
    }
    // Cache: BSSID pin
    if (kCacheBssid && g_have_bssid) {
      wifi.sta.bssid_set = true;
      std::memcpy(wifi.sta.bssid, g_bssid, sizeof(wifi.sta.bssid));
    }

    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wifi);
    esp_wifi_set_protocol(
        WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
    esp_wifi_start();
    // PS_NONE through association / GOT_IP — MAX_MODEM before GOT_IP can stall
    // connect and yield silent wakes.
    esp_wifi_set_ps(WIFI_PS_NONE);

    // Bounded GOT_IP (10s + one reconnect nudge); avoid portMAX_DELAY wedging.
    bool const got_ip = WaitGotIpBounded();
    // Freeze link: no auto-reconnect during settle/hold/learn/teardown.
    g_want_reconnect = false;

    if (got_ip) {
      // Modem PS only after link is up (settle → send → hold).
      esp_wifi_set_ps(WIFI_PS_MAX_MODEM);

      // Cache: static ARP (peer / gateway)
      if (kCachePeerArp && g_have_peer_mac) {
        (void)InstallStaticArp(g_peer_ip, g_peer_mac);
      }
      if (kCacheGwArp && g_have_gw_mac && g_gateway != 0) {
        (void)InstallStaticArp(g_gateway, g_gw_mac);
      }

      // Periodic GARP stays off in sdkconfig; one announce refreshes AP ARP.
      SendOneShotGar(netif);

      vTaskDelay(pdMS_TO_TICKS(50));  // settle after GOT_IP (keep fixed)

      (void)esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true,
                                           WIFI_PHY_RATE_1M_L);
      (void)esp_wifi_internal_set_retry_counter(3, 3);

      UdpSendCtx send{};
      (void)ipaddr_aton(AE_UDP_SERVER_HOST, &send.dest_ip);
      send.port = static_cast<std::uint16_t>(AE_UDP_SERVER_PORT);
      send.payload[0] = static_cast<std::uint8_t>(g_index & 0xffu);
      send.payload[1] = static_cast<std::uint8_t>((g_index >> 8) & 0xffu);
      send.payload[2] = static_cast<std::uint8_t>((g_index >> 16) & 0xffu);
      send.payload[3] = static_cast<std::uint8_t>((g_index >> 24) & 0xffu);
      std::uint32_t const peer_ip = ip_2_ip4(&send.dest_ip)->addr;

      (void)esp_netif_tcpip_exec(&UdpSendTcpip, &send);
      TickType_t const hold_deadline =
          xTaskGetTickCount() + pdMS_TO_TICKS(200);  // post-send hold
      ++g_index;

      // Learn enabled caches once (cold → hot). Overlap ARP resolve with hold.
      if (kCacheChannel && !g_have_channel) {
        wifi_ap_record_t ap{};
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
          g_channel = ap.primary;
          g_have_channel = 1;
        }
      }
      if (kCacheBssid && !g_have_bssid) {
        wifi_ap_record_t ap{};
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
          std::memcpy(g_bssid, ap.bssid, sizeof(g_bssid));
          g_have_bssid = 1;
        }
      }
      if (kCacheStaticIp && !g_have_static_ip) {
        esp_netif_ip_info_t ip_info{};
        esp_netif_get_ip_info(netif, &ip_info);
        g_ip = ip_info.ip.addr;
        g_netmask = ip_info.netmask.addr;
        g_gateway = ip_info.gw.addr;
        g_have_static_ip = 1;
      }
      if (kCachePeerArp && !g_have_peer_mac) {
        g_peer_ip = peer_ip;
        if (ResolveMac(netif, peer_ip, g_peer_mac, kArpLearnTimeoutMs)) {
          g_have_peer_mac = 1;
        }
      }
      if (kCacheGwArp && !g_have_gw_mac) {
        if (g_gateway == 0) {
          esp_netif_ip_info_t ip_info{};
          esp_netif_get_ip_info(netif, &ip_info);
          g_gateway = ip_info.gw.addr;
        }
        if (g_gateway != 0 &&
            ResolveMac(netif, g_gateway, g_gw_mac, kArpLearnTimeoutMs)) {
          g_have_gw_mac = 1;
        }
      }

      // Finish remaining post-send hold (never shorter than 200 ms).
      TickType_t const now = xTaskGetTickCount();
      if (now < hold_deadline) {
        vTaskDelay(hold_deadline - now);
      }

      // Drop static ARP before stop/deinit (avoid sticky tcpip work).
      if (kCachePeerArp && g_have_peer_mac) {
        RemoveStaticArp(g_peer_ip);
      }
      if (kCacheGwArp && g_have_gw_mac && g_gateway != 0) {
        RemoveStaticArp(g_gateway);
      }
    }

    // Full teardown
    esp_event_handler_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnWifiEvent);
    esp_event_handler_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, &OnWifiEvent);
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

    PeripheralPowerDownForDeepSleep();
    esp_sleep_enable_timer_wakeup(kSleepUs);
    // Do not esp_sleep_enable_ulp_wakeup(); timer deep sleep only.
    esp_deep_sleep_start();
  }
}

#endif  // ESP_PLATFORM && AETHER_DIAG_UDP_RTC_INDEX
