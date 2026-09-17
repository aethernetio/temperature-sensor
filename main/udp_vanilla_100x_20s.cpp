/*
 * Copyright 2026 Aethernet Inc.
 *
 * Vanilla ESP-IDF Wi-Fi UDP baseline (AETHER_DIAG_UDP_VANILLA_100X_20S):
 *   cold STA + DHCP each wake, WIFI_PS_NONE, no RTC Wi-Fi caches,
 *   no internal rate/retry APIs, settle 100 ms, post-send hold 1000 ms,
 *   verified GPIO shutdown, 20 s timer deep sleep × 100 cycles.
 *
 * Cold boot / non-TIMER wake: 60 s awake flash window before first cycle.
 */

#include "udp_vanilla_100x_20s.h"

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_UDP_VANILLA_100X_20S)

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
#  include <esp_sleep.h>
#  include <esp_task_wdt.h>
#  include <esp_wifi.h>
#  include <nvs_flash.h>

#  include <lwip/ip_addr.h>
#  include <lwip/pbuf.h>
#  include <lwip/udp.h>

#  include "boards/aether_esp32_c6.h"

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
constexpr int kSettleAfterGotIpMs = 100;
constexpr int kPostSendHoldMs = 1000;
constexpr int kGotIpTimeoutMs = 20000;
constexpr std::uint32_t kMeasuredCycles = 100;
constexpr std::uint64_t kSleepUs = 20ULL * 1000000ULL;
constexpr std::uint64_t kDoneSleepUs = 60ULL * 60ULL * 1000000ULL;
constexpr int kColdBootFlashWindowS = 60;
constexpr std::uint32_t kDumpMagic = 0x314E4156u;  // 'VAN1' LE

#pragma pack(push, 1)
struct CycleRec {
  std::uint16_t cycle;       // 1..100
  std::uint16_t got_ip_ms;   // 0xFFFF if fail
  std::int8_t send_err;      // lwIP err_t; 127 = not called
  std::uint8_t flags;        // bit0 got_ip, bit1 send_called
  std::uint16_t pad;
};
#pragma pack(pop)

static_assert(sizeof(CycleRec) == 8, "CycleRec size");

RTC_DATA_ATTR std::uint32_t g_cycle = 0;  // completed cycles (0 before first)
RTC_DATA_ATTR CycleRec g_recs[kMeasuredCycles] = {};
RTC_DATA_ATTR std::uint8_t g_dump_done = 0;

EventGroupHandle_t g_events = nullptr;
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

struct UdpSendCtx {
  ip_addr_t dest_ip{};
  std::uint16_t port{0};
  std::uint8_t payload[4]{};
  err_t err{ERR_OK};
};

esp_err_t UdpSendTcpip(void* arg) {
  auto* c = static_cast<UdpSendCtx*>(arg);
  struct udp_pcb* upcb = udp_new();
  if (upcb == nullptr) {
    c->err = ERR_MEM;
    return ESP_OK;
  }
  struct pbuf* p = pbuf_alloc(PBUF_TRANSPORT, sizeof(c->payload), PBUF_RAM);
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

struct UdpDumpCtx {
  ip_addr_t dest_ip{};
  std::uint16_t port{0};
  err_t err{ERR_OK};
};

esp_err_t UdpDumpTcpip(void* arg) {
  auto* c = static_cast<UdpDumpCtx*>(arg);
  // Header: magic(4) + count(2) + offset(2) + up to 12 records (96 B) per pkt.
  constexpr std::size_t kPerPkt = 12;
  std::uint16_t offset = 0;
  while (offset < kMeasuredCycles) {
    std::uint16_t n = static_cast<std::uint16_t>(kMeasuredCycles - offset);
    if (n > kPerPkt) {
      n = static_cast<std::uint16_t>(kPerPkt);
    }
    std::size_t const bytes =
        8u + static_cast<std::size_t>(n) * sizeof(CycleRec);
    struct udp_pcb* upcb = udp_new();
    if (upcb == nullptr) {
      c->err = ERR_MEM;
      return ESP_OK;
    }
    struct pbuf* p = pbuf_alloc(PBUF_TRANSPORT, bytes, PBUF_RAM);
    if (p == nullptr) {
      udp_remove(upcb);
      c->err = ERR_MEM;
      return ESP_OK;
    }
    auto* raw = static_cast<std::uint8_t*>(p->payload);
    std::memcpy(raw + 0, &kDumpMagic, 4);
    std::memcpy(raw + 4, &n, 2);
    std::memcpy(raw + 6, &offset, 2);
    std::memcpy(raw + 8, &g_recs[offset], n * sizeof(CycleRec));
    c->err = udp_sendto(upcb, p, &c->dest_ip, c->port);
    pbuf_free(p);
    udp_remove(upcb);
    if (c->err != ERR_OK) {
      return ESP_OK;
    }
    offset = static_cast<std::uint16_t>(offset + n);
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  return ESP_OK;
}

bool WaitGotIpOnce(int timeout_ms) {
  EventBits_t bits = xEventGroupWaitBits(
      g_events, kGotIpBit, pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms));
  return (bits & kGotIpBit) != 0;
}

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

void VerifiedPeripheralShutdownForDeepSleep() {
  ConfigOutputLevelHold(GPIO_NUM_17, 0);
  ConfigOutputLevelHold(GPIO_NUM_2, 1);
  ConfigDisableNoPull(GPIO_NUM_18);
  ConfigDisableNoPull(GPIO_NUM_6);
  ConfigDisableNoPull(GPIO_NUM_7);
}

void PrepareDeepSleep() {
  VerifiedPeripheralShutdownForDeepSleep();
  (void)esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
}

void TeardownWifi(esp_netif_t* netif) {
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
}

bool BringUpWifi(esp_netif_t** out_netif) {
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

  wifi_config_t wifi{};
  std::strncpy(reinterpret_cast<char*>(wifi.sta.ssid), WIFI_SSID,
               sizeof(wifi.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wifi.sta.password), WIFI_PASSWORD,
               sizeof(wifi.sta.password));
  wifi.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  // Vanilla: no channel / BSSID pin.

  esp_wifi_set_mode(WIFI_MODE_STA);
  esp_wifi_set_config(WIFI_IF_STA, &wifi);
  esp_wifi_start();
  // PS_NONE for entire connect/send/hold until stop.
  esp_wifi_set_ps(WIFI_PS_NONE);
  *out_netif = netif;
  return true;
}

void RecordAndSleep(CycleRec const& rec) {
  if (rec.cycle >= 1 && rec.cycle <= kMeasuredCycles) {
    g_recs[rec.cycle - 1] = rec;
  }
  g_cycle = rec.cycle;
  PrepareDeepSleep();
  esp_sleep_enable_timer_wakeup(kSleepUs);
  esp_deep_sleep_start();
}

void RunDiagDumpThenDone() {
  if (g_dump_done) {
    PrepareDeepSleep();
    esp_sleep_enable_timer_wakeup(kDoneSleepUs);
    esp_deep_sleep_start();
  }
  esp_netif_t* netif = nullptr;
  BringUpWifi(&netif);
  bool const got = WaitGotIpOnce(kGotIpTimeoutMs);
  g_want_reconnect = false;
  if (got) {
    UdpDumpCtx dump{};
    (void)ipaddr_aton(AE_UDP_SERVER_HOST, &dump.dest_ip);
    dump.port = static_cast<std::uint16_t>(AE_UDP_SERVER_PORT);
    (void)esp_netif_tcpip_exec(&UdpDumpTcpip, &dump);
    vTaskDelay(pdMS_TO_TICKS(500));
  }
  TeardownWifi(netif);
  g_dump_done = 1;
  PrepareDeepSleep();
  esp_sleep_enable_timer_wakeup(kDoneSleepUs);
  esp_deep_sleep_start();
}

}  // namespace

extern "C" void RunUdpVanilla100x20s() {
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_TIMER) {
    (void)esp_task_wdt_delete(xTaskGetCurrentTaskHandle());
    for (int s = 0; s < kColdBootFlashWindowS; ++s) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }

  for (;;) {
    if (g_cycle >= kMeasuredCycles) {
      RunDiagDumpThenDone();
    }

    std::uint32_t const cycle_id = g_cycle + 1;  // 1..100
    CycleRec rec{};
    rec.cycle = static_cast<std::uint16_t>(cycle_id);
    rec.got_ip_ms = 0xFFFFu;
    rec.send_err = 127;
    rec.flags = 0;
    rec.pad = 0;

    esp_netif_t* netif = nullptr;
    BringUpWifi(&netif);

    std::uint64_t const t0 = esp_rtc_get_time_us();
    bool const got_ip = WaitGotIpOnce(kGotIpTimeoutMs);
    g_want_reconnect = false;

    if (got_ip) {
      rec.flags = static_cast<std::uint8_t>(rec.flags | 0x01u);
      std::uint64_t const dt_us = esp_rtc_get_time_us() - t0;
      std::uint32_t ms = static_cast<std::uint32_t>(dt_us / 1000ULL);
      if (ms > 0xFFFEu) {
        ms = 0xFFFEu;
      }
      rec.got_ip_ms = static_cast<std::uint16_t>(ms);

      // Keep WIFI_PS_NONE (already set). Conservative settle, then UDP.
      vTaskDelay(pdMS_TO_TICKS(kSettleAfterGotIpMs));

      UdpSendCtx send{};
      (void)ipaddr_aton(AE_UDP_SERVER_HOST, &send.dest_ip);
      send.port = static_cast<std::uint16_t>(AE_UDP_SERVER_PORT);
      send.payload[0] = static_cast<std::uint8_t>(cycle_id & 0xffu);
      send.payload[1] = static_cast<std::uint8_t>((cycle_id >> 8) & 0xffu);
      send.payload[2] = static_cast<std::uint8_t>((cycle_id >> 16) & 0xffu);
      send.payload[3] = static_cast<std::uint8_t>((cycle_id >> 24) & 0xffu);

      rec.flags = static_cast<std::uint8_t>(rec.flags | 0x02u);
      (void)esp_netif_tcpip_exec(&UdpSendTcpip, &send);
      rec.send_err = static_cast<std::int8_t>(send.err);

      vTaskDelay(pdMS_TO_TICKS(kPostSendHoldMs));
    }

    TeardownWifi(netif);
    RecordAndSleep(rec);
  }
}

#endif  // ESP_PLATFORM && AETHER_DIAG_UDP_VANILLA_100X_20S
