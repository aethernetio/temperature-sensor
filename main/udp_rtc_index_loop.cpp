/*
 * Copyright 2026 Aethernet Inc.
 *
 * UDP RTC index path (AETHER_DIAG_UDP_RTC_INDEX and
 * AETHER_DIAG_UDP_LOW_POWER_1MIN_10).
 *
 * Shared: wait GOT_IP → post-connect PS → settle → udp_sendto → hold →
 *   full teardown → BoardPowerDownForDeepSleep → timer deep sleep
 *   PS_NONE through association/GOT_IP; Wi-Fi 4 + fixed 1M; retry 3,3
 *
 * Combined 1-min mode: warmup #0 then N measured sends at 60 s
 * start-to-start (default N=100). After teardown: verified GPIO shutdown
 * (17 LOW, 2 HIGH, 18/6/7 disabled) then timer deep sleep.
 * No esp_sleep_pd_config / LP-core.
 *
 * Cold boot / non-TIMER wake: remain awake 60 s before first UDP cycle so
 * USB/COM stays flashable. Timer deep-sleep wakes skip that wait.
 */

#include "udp_rtc_index_loop.h"

#if defined(ESP_PLATFORM) && \
    (defined(AETHER_DIAG_UDP_RTC_INDEX) || \
     defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10))

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
#  include <esp_task_wdt.h>
#  include <esp_wifi.h>
#  include <nvs_flash.h>
#  include <sdkconfig.h>
#  include <soc/soc.h>
#  include <soc/soc_caps.h>

#  if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
// Verified board shutdown lives in this file (no BoardPrepareDeepSleep / PD / LP-core).
#  else
#    include "sleeping/board_sleep_powerdown.h"
#  endif

#  include <lwip/etharp.h>
#  include <lwip/ip_addr.h>
#  include <lwip/netif.h>
#  include <lwip/pbuf.h>
#  include <lwip/udp.h>

// BOARD_AETHER_ESP32_C6 pins from aethernetio/temperature-sensor @ ef5da3b
// (main/boards/aether_esp32_c6.h). UDP diag does not link aether/user_config.
#  include "boards/aether_esp32_c6.h"

// STATUS_LED_ON_OFF_LEVEL comes from aether_esp32_c6.h (OFF=LOW, bisect B1).


extern "C" esp_err_t esp_wifi_internal_set_retry_counter(uint8_t short_retry,
                                                         uint8_t long_retry);
extern "C" std::uint64_t esp_rtc_get_time_us(void);

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
constexpr std::uint32_t kMeasuredSends = AE_UDP_MEASURED_SENDS;
constexpr std::uint64_t kPeriodUs = 60ULL * 1000000ULL;
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

void PeripheralPowerDownForDeepSleep() {
#  if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
  // Unused in 1-min combined path; PrepareCombinedDeepSleep owns shutdown.
#  else
  BoardPowerDownForDeepSleep();
#  endif
}

#if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
void ConfigOutputLevelHold(gpio_num_t pin, int level) {
  (void)gpio_hold_dis(pin);
  gpio_config_t cfg = {};
  cfg.pin_bit_mask = 1ULL << static_cast<unsigned>(pin);
  cfg.mode = GPIO_MODE_OUTPUT;
  cfg.pull_up_en = GPIO_PULLUP_DISABLE;
  cfg.pull_down_en = GPIO_PULLDOWN_DISABLE;
  cfg.intr_type = GPIO_INTR_DISABLE;
  (void)gpio_config(&cfg);
  (void)gpio_set_level(pin, level);
  (void)gpio_hold_en(pin);
}

void ConfigDisableNoPull(gpio_num_t pin) {
  (void)gpio_hold_dis(pin);
  gpio_config_t cfg = {};
  cfg.pin_bit_mask = 1ULL << static_cast<unsigned>(pin);
  cfg.mode = GPIO_MODE_DISABLE;
  cfg.pull_up_en = GPIO_PULLUP_DISABLE;
  cfg.pull_down_en = GPIO_PULLDOWN_DISABLE;
  cfg.intr_type = GPIO_INTR_DISABLE;
  (void)gpio_config(&cfg);
}

/* Hand-verified Thermometer 2 sleep pinout (no LP-core regs, no pd_config). */
void VerifiedPeripheralShutdownForDeepSleep() {
  // GPIO17 STATUS_LED_ON: LOW + hold (cuts LED rail draw).
  ConfigOutputLevelHold(GPIO_NUM_17, 0);
  // GPIO2 PWR_ON: HIGH + hold (LOW raises sleep to ~mA on this unit).
  ConfigOutputLevelHold(GPIO_NUM_2, 1);
  // GPIO18 / I2C: fully disable.
  ConfigDisableNoPull(GPIO_NUM_18);
  ConfigDisableNoPull(GPIO_NUM_6);
  ConfigDisableNoPull(GPIO_NUM_7);
}

void PrepareCombinedDeepSleep() {
  VerifiedPeripheralShutdownForDeepSleep();
  (void)esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
}

void QuietRailsIfNeeded() {
  // Do not force sleep pinout before Wi-Fi; apply only after teardown.
}

void EnterDoneSleep() {
  PrepareCombinedDeepSleep();
  esp_sleep_enable_timer_wakeup(kDoneSleepUs);
  esp_deep_sleep_start();
}
#endif

}  // namespace

extern "C" void RunUdpRtcIndexLoop() {
#if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
  // Flash window after power-on / reset only — never after timer deep sleep.
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_TIMER) {
    (void)esp_task_wdt_delete(xTaskGetCurrentTaskHandle());
    for (int s = 0; s < kColdBootFlashWindowS; ++s) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }
#endif
  for (;;) {
    // ------------------------------------------------------------------
    // One wake cycle: keep rails off → caches → GOT_IP → settle → send → sleep
    // ------------------------------------------------------------------
#if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
    if (g_index > kMeasuredSends) {
      EnterDoneSleep();
    }
    QuietRailsIfNeeded();
    std::uint64_t const cycle_start_us = esp_rtc_get_time_us();
    bool const warmup = (g_index == 0);
    if (!warmup && !g_have_deadline) {
      g_next_deadline_us = cycle_start_us + kPeriodUs;
      g_have_deadline = 1;
    }
#else
    // Reassert OFF after wake without enabling peripherals.
    PeripheralPowerDownForDeepSleep();
#endif
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

      vTaskDelay(pdMS_TO_TICKS(kPreSettleMs));  // settle after GOT_IP

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
          xTaskGetTickCount() + pdMS_TO_TICKS(kPostSendHoldMs);
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

      // Finish remaining post-send hold (never shorter than kPostSendHoldMs).
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
#if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
    } else if (g_index > 0) {
      // Measured slot with no GOT_IP: still consume the attempt (no retry burst).
      ++g_index;
#endif
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

#if defined(AETHER_DIAG_UDP_LOW_POWER_1MIN_10)
    PrepareCombinedDeepSleep();
    {
      std::uint64_t const now_us = esp_rtc_get_time_us();
      std::uint64_t sleep_us = kWarmupSleepUs;
#  if defined(AE_COMBINED_LONG_SLEEP_AFTER_FIRST) && \
      (AE_COMBINED_LONG_SLEEP_AFTER_FIRST)
      // Root-cause diag: after first measured UDP (g_index>=2), sleep 10 min.
      if (g_index >= 2) {
        sleep_us = 10ULL * 60ULL * 1000000ULL;
        esp_sleep_enable_timer_wakeup(sleep_us);
        esp_deep_sleep_start();
      }
#  endif
      // Warmup (#0) or just-finished warmup (g_index==1, deadline not armed).
      if (g_index > 1 || g_have_deadline) {
        if (now_us >= g_next_deadline_us) {
          g_next_deadline_us = now_us + kPeriodUs;
          ++g_overrun;
        }
        sleep_us = g_next_deadline_us - now_us;
        g_next_deadline_us += kPeriodUs;
      }
      if (sleep_us < 1000ULL) {
        sleep_us = 1000ULL;
      }
      esp_sleep_enable_timer_wakeup(sleep_us);
    }
#else
    PeripheralPowerDownForDeepSleep();
    esp_sleep_enable_timer_wakeup(kSleepUs);
#endif
    // Do not esp_sleep_enable_ulp_wakeup(); timer deep sleep only.
    esp_deep_sleep_start();
  }
}

#endif  // ESP_PLATFORM && (UDP_RTC_INDEX || UDP_LOW_POWER_1MIN_10)
