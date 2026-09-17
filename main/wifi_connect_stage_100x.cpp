/*
 * Copyright 2026 Aethernet Inc.
 *
 * Cold Wi-Fi connect stage diagnostic (AETHER_DIAG_WIFI_CONNECT_STAGE_100X):
 *   vanilla STA + DHCP, WIFI_PS_NONE, no caches, one connect() per wake,
 *   no UDP, 20 s observe window, 2 s deep sleep × 100 cycles.
 *   Results in RTC; CSV dump over USB Serial/JTAG after cycle 100.
 */

#include "wifi_connect_stage_100x.h"

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_WIFI_CONNECT_STAGE_100X)

#  include <cstdint>
#  include <cstdio>
#  include <cstring>

#  include <freertos/FreeRTOS.h>
#  include <freertos/event_groups.h>
#  include <freertos/task.h>

#  include <driver/gpio.h>
#  include <esp_attr.h>
#  include <esp_event.h>
#  include <esp_netif.h>
#  include <esp_sleep.h>
#  include <esp_task_wdt.h>
#  include <esp_wifi.h>
#  include <nvs_flash.h>

#  include "boards/aether_esp32_c6.h"

extern "C" std::uint64_t esp_rtc_get_time_us(void);

#  ifndef WIFI_SSID
#    error "WIFI_SSID required"
#  endif
#  ifndef WIFI_PASSWORD
#    error "WIFI_PASSWORD required"
#  endif

namespace {

constexpr int kEvtStart = BIT0;
constexpr int kEvtConnected = BIT1;
constexpr int kEvtGotIp = BIT2;
constexpr int kEvtDisc = BIT3;

constexpr int kObserveTimeoutMs = 20000;
constexpr std::uint32_t kMeasuredCycles = 100;
constexpr std::uint64_t kSleepUs = 2ULL * 1000000ULL;
constexpr int kColdBootFlashWindowS = 60;
constexpr int kDumpHoldS = 180;

enum ResultClass : std::uint8_t {
  kGotIpSuccess = 0,
  kStaConnectedButNoGotIp = 1,
  kConnectedThenDiscBeforeIp = 2,
  kDiscBeforeStaConnected = 3,
  kTimeoutNoTerminal = 4,
};

#pragma pack(push, 1)
struct CycleRec {
  std::uint8_t flags;  // bit0 start, bit1 connected, bit2 got_ip, bit3 disc
  std::uint8_t result_class;
  std::uint8_t disc_count;
  std::uint8_t first_reason;
  std::uint8_t last_reason;
  std::uint8_t reasons[3];
  std::uint16_t wifi_start_ms;
  std::uint16_t sta_connected_ms;  // 0xFFFF = none
  std::uint16_t got_ip_ms;         // 0xFFFF = none
  std::uint8_t dhcp_status;        // 0=unknown/NA, else esp_netif_dhcp_status_t+1
  std::uint8_t pad;
};
#pragma pack(pop)

static_assert(sizeof(CycleRec) == 16, "CycleRec size");

RTC_DATA_ATTR std::uint32_t g_cycle = 0;
RTC_DATA_ATTR CycleRec g_recs[kMeasuredCycles] = {};

EventGroupHandle_t g_events = nullptr;
std::uint64_t g_t0_us = 0;
bool g_connect_issued = false;
CycleRec* g_cur = nullptr;

std::uint16_t ElapsedMs() {
  std::uint64_t const dt = esp_rtc_get_time_us() - g_t0_us;
  std::uint32_t ms = static_cast<std::uint32_t>(dt / 1000ULL);
  if (ms > 0xFFFEu) {
    ms = 0xFFFEu;
  }
  return static_cast<std::uint16_t>(ms);
}

void NoteDisconnect(std::uint8_t reason) {
  if (g_cur == nullptr) {
    return;
  }
  g_cur->flags = static_cast<std::uint8_t>(g_cur->flags | 0x08u);
  if (g_cur->disc_count == 0) {
    g_cur->first_reason = reason;
  }
  g_cur->last_reason = reason;
  if (g_cur->disc_count < 3) {
    g_cur->reasons[g_cur->disc_count] = reason;
  }
  if (g_cur->disc_count < 255) {
    ++g_cur->disc_count;
  }
}

void OnWifiEvent(void*, esp_event_base_t base, std::int32_t id, void* data) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    if (g_cur != nullptr) {
      g_cur->flags = static_cast<std::uint8_t>(g_cur->flags | 0x01u);
      g_cur->wifi_start_ms = ElapsedMs();
    }
    xEventGroupSetBits(g_events, kEvtStart);
    if (!g_connect_issued) {
      g_connect_issued = true;
      (void)esp_wifi_connect();
    }
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
    if (g_cur != nullptr) {
      g_cur->flags = static_cast<std::uint8_t>(g_cur->flags | 0x02u);
      g_cur->sta_connected_ms = ElapsedMs();
    }
    xEventGroupSetBits(g_events, kEvtConnected);
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    auto const* ev = static_cast<wifi_event_sta_disconnected_t const*>(data);
    std::uint8_t reason = 0;
    if (ev != nullptr) {
      reason = static_cast<std::uint8_t>(ev->reason);
    }
    NoteDisconnect(reason);
    xEventGroupSetBits(g_events, kEvtDisc);
    // One-shot: do NOT reconnect.
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    if (g_cur != nullptr) {
      g_cur->flags = static_cast<std::uint8_t>(g_cur->flags | 0x04u);
      g_cur->got_ip_ms = ElapsedMs();
    }
    xEventGroupSetBits(g_events, kEvtGotIp);
  }
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

void Teardown(esp_netif_t* netif) {
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

ResultClass Classify(CycleRec const& r) {
  bool const connected = (r.flags & 0x02u) != 0;
  bool const got_ip = (r.flags & 0x04u) != 0;
  bool const disc = (r.flags & 0x08u) != 0;
  if (got_ip) {
    return kGotIpSuccess;
  }
  if (connected && disc) {
    return kConnectedThenDiscBeforeIp;
  }
  if (connected && !got_ip) {
    return kStaConnectedButNoGotIp;
  }
  if (disc && !connected) {
    return kDiscBeforeStaConnected;
  }
  return kTimeoutNoTerminal;
}

char const* ClassName(std::uint8_t c) {
  switch (c) {
    case kGotIpSuccess:
      return "GOT_IP_SUCCESS";
    case kStaConnectedButNoGotIp:
      return "STA_CONNECTED_BUT_NO_GOT_IP";
    case kConnectedThenDiscBeforeIp:
      return "CONNECTED_THEN_DISCONNECTED_BEFORE_IP";
    case kDiscBeforeStaConnected:
      return "DISCONNECTED_BEFORE_STA_CONNECTED";
    default:
      return "TIMEOUT_NO_TERMINAL_EVENT";
  }
}

void DumpAndHold() {
  (void)esp_task_wdt_delete(xTaskGetCurrentTaskHandle());
  auto print_dump = []() {
    std::printf("BEGIN_WIFI_CONNECT_STAGE_DUMP\n");
    std::printf(
        "cycle,wifi_start,sta_connected,got_ip,disconnected,disconnect_count,"
        "first_disconnect_reason,final_disconnect_reason,association_time_ms,"
        "dhcp_time_ms,total_connect_time_ms,result_class,dhcp_status\n");
    for (std::uint32_t i = 0; i < kMeasuredCycles; ++i) {
      CycleRec const& r = g_recs[i];
      int const wifi_start = (r.flags & 0x01u) ? 1 : 0;
      int const sta_conn = (r.flags & 0x02u) ? 1 : 0;
      int const got_ip = (r.flags & 0x04u) ? 1 : 0;
      int const disc = (r.flags & 0x08u) ? 1 : 0;
      int assoc = -1;
      int dhcp = -1;
      int total = -1;
      if (sta_conn && r.sta_connected_ms != 0xFFFF &&
          r.wifi_start_ms != 0xFFFF) {
        assoc = static_cast<int>(r.sta_connected_ms) -
                static_cast<int>(r.wifi_start_ms);
        if (assoc < 0) {
          assoc = -1;
        }
      }
      if (got_ip && r.got_ip_ms != 0xFFFF && r.sta_connected_ms != 0xFFFF) {
        dhcp = static_cast<int>(r.got_ip_ms) -
               static_cast<int>(r.sta_connected_ms);
        if (dhcp < 0) {
          dhcp = -1;
        }
      }
      if (got_ip && r.got_ip_ms != 0xFFFF && r.wifi_start_ms != 0xFFFF) {
        total =
            static_cast<int>(r.got_ip_ms) - static_cast<int>(r.wifi_start_ms);
        if (total < 0) {
          total = -1;
        }
      }
      std::printf(
          "%u,%d,%d,%d,%d,%u,%u,%u,%d,%d,%d,%s,%u\n",
          static_cast<unsigned>(i + 1), wifi_start, sta_conn, got_ip, disc,
          static_cast<unsigned>(r.disc_count),
          static_cast<unsigned>(r.first_reason),
          static_cast<unsigned>(r.last_reason), assoc, dhcp, total,
          ClassName(r.result_class), static_cast<unsigned>(r.dhcp_status));
    }
    std::printf("END_WIFI_CONNECT_STAGE_DUMP\n");
    std::fflush(stdout);
  };
  // Re-print periodically so USB re-enumeration can still catch the dump.
  for (int s = 0; s < kDumpHoldS; ++s) {
    if ((s % 30) == 0) {
      print_dump();
    }
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
  PrepareDeepSleep();
  esp_sleep_enable_timer_wakeup(60ULL * 60ULL * 1000000ULL);
  esp_deep_sleep_start();
}

void RunOneCycle(std::uint32_t cycle_id) {
  CycleRec rec{};
  rec.flags = 0;
  rec.result_class = kTimeoutNoTerminal;
  rec.disc_count = 0;
  rec.first_reason = 0;
  rec.last_reason = 0;
  rec.reasons[0] = rec.reasons[1] = rec.reasons[2] = 0;
  rec.wifi_start_ms = 0xFFFF;
  rec.sta_connected_ms = 0xFFFF;
  rec.got_ip_ms = 0xFFFF;
  rec.dhcp_status = 0;
  rec.pad = 0;
  g_cur = &rec;
  g_connect_issued = false;

  nvs_flash_init();
  esp_netif_init();
  esp_event_loop_create_default();
  esp_netif_t* netif = esp_netif_create_default_wifi_sta();
  g_events = xEventGroupCreate();
  xEventGroupClearBits(g_events, 0xFFu);

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

  esp_wifi_set_mode(WIFI_MODE_STA);
  esp_wifi_set_config(WIFI_IF_STA, &wifi);
  g_t0_us = esp_rtc_get_time_us();
  esp_wifi_start();
  esp_wifi_set_ps(WIFI_PS_NONE);

  TickType_t const deadline =
      xTaskGetTickCount() + pdMS_TO_TICKS(kObserveTimeoutMs);
  for (;;) {
    TickType_t const now = xTaskGetTickCount();
    if (now >= deadline) {
      break;
    }
    TickType_t wait = deadline - now;
    EventBits_t bits = xEventGroupWaitBits(
        g_events, kEvtGotIp | kEvtDisc | kEvtConnected | kEvtStart, pdTRUE,
        pdFALSE, wait);
    if ((bits & kEvtGotIp) != 0) {
      break;
    }
    if ((bits & kEvtDisc) != 0) {
      // Terminal for one-shot attempt once we have disc outcome relative to
      // connection state (may still be waiting if disc before start — rare).
      if ((rec.flags & 0x02u) != 0 || (rec.flags & 0x01u) != 0) {
        break;
      }
    }
  }

  // DHCP status snapshot if connected.
  if ((rec.flags & 0x02u) != 0) {
    esp_netif_dhcp_status_t st = ESP_NETIF_DHCP_INIT;
    if (esp_netif_dhcpc_get_status(netif, &st) == ESP_OK) {
      rec.dhcp_status = static_cast<std::uint8_t>(st) + 1u;
    }
  }

  rec.result_class = static_cast<std::uint8_t>(Classify(rec));
  g_recs[cycle_id - 1] = rec;
  g_cur = nullptr;
  g_cycle = cycle_id;

  Teardown(netif);
  PrepareDeepSleep();
  esp_sleep_enable_timer_wakeup(kSleepUs);
  esp_deep_sleep_start();
}

}  // namespace

extern "C" void RunWifiConnectStage100x() {
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_TIMER) {
    (void)esp_task_wdt_delete(xTaskGetCurrentTaskHandle());
    for (int s = 0; s < kColdBootFlashWindowS; ++s) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }

  for (;;) {
    if (g_cycle >= kMeasuredCycles) {
      DumpAndHold();
    }
    RunOneCycle(g_cycle + 1);
  }
}

#endif  // ESP_PLATFORM && AETHER_DIAG_WIFI_CONNECT_STAGE_100X
