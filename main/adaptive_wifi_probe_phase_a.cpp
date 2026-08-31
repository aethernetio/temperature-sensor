/*
 * Copyright 2026 Aethernet Inc.
 *
 * Phase A: ICMP gateway probe matrix + PRE search (no Aether server).
 * Machine lines: A_RES / A_SUM / A_PRE / A_WINNER / A_DONE
 */

#include <array>
#include <cstdio>
#include <cstring>

#include <esp_event.h>
#include <esp_netif.h>
#include <esp_timer.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <nvs_flash.h>

#include "aether/wifi/wifi_gateway_probe.h"
#include "aether/wifi/wifi_probe_state.h"

#ifndef WIFI_SSID
#  error "WIFI_SSID required"
#endif
#ifndef WIFI_PASSWORD
#  error "WIFI_PASSWORD required"
#endif

#ifndef AE_PROBE_RECONNECTS
#  define AE_PROBE_RECONNECTS 30
#endif
#ifndef AE_PROBE_ICMP_PER
#  define AE_PROBE_ICMP_PER 3
#endif
#ifndef AE_PROBE_PRE_RECONNECTS
#  define AE_PROBE_PRE_RECONNECTS 20
#endif
#ifndef AE_PROBE_PASS_PCT
#  define AE_PROBE_PASS_PCT 98
#endif

namespace {

constexpr int kWifiConnectedBit = BIT0;
constexpr int kWifiFailBit = BIT1;

EventGroupHandle_t g_eg = nullptr;
esp_netif_t* g_netif = nullptr;
int g_retry = 0;
bool g_wait_ip = true;

struct LocalCache {
  std::uint8_t channel{0};
  std::uint32_t ip{0};
  std::uint32_t netmask{0};
  std::uint32_t gateway{0};
  std::uint8_t bssid[6]{};
  std::uint8_t gw_mac[6]{};
  std::uint8_t authmode{0};
  std::uint8_t flags{0};  // bit0=ip bit1=ch bit2=gw_mac
};

RTC_DATA_ATTR LocalCache g_cache{};
RTC_DATA_ATTR ae::WifiProbeRtcState g_probe{};

struct RunStats {
  int connect_ok{0};
  int ready_ok{0};
  int icmp_sent{0};
  int icmp_recv{0};
  int fail{0};
  std::array<std::uint32_t, 64> connect_ms{};
  int n_samples{0};
};

void OnWifi(void*, esp_event_base_t base, int32_t id, void*) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    esp_wifi_connect();
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    if (++g_retry < 10) {
      esp_wifi_connect();
    } else if (g_eg) {
      xEventGroupSetBits(g_eg, kWifiFailBit);
    }
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
    if (!g_wait_ip && g_eg) {
      xEventGroupSetBits(g_eg, kWifiConnectedBit);
    }
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    if (g_eg) {
      xEventGroupSetBits(g_eg, kWifiConnectedBit);
    }
  }
}

void Teardown() {
  esp_event_handler_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnWifi);
  esp_event_handler_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, &OnWifi);
  esp_wifi_disconnect();
  esp_wifi_stop();
  esp_wifi_deinit();
  if (g_netif) {
    esp_netif_destroy_default_wifi(g_netif);
    g_netif = nullptr;
  }
  if (g_eg) {
    vEventGroupDelete(g_eg);
    g_eg = nullptr;
  }
  esp_event_loop_delete_default();
}

bool ConnectProfile(ae::WifiProbeProfile profile, std::uint16_t pre_ms,
                    std::int64_t* connect_us, std::uint32_t* gw_be) {
  *connect_us = 0;
  *gw_be = 0;
  g_retry = 0;
  g_wait_ip = !(ae::WifiProbeProfileUsesCachedIp(profile) && g_cache.ip != 0);

  nvs_flash_init();
  esp_netif_init();
  esp_event_loop_create_default();
  g_eg = xEventGroupCreate();
  g_netif = esp_netif_create_default_wifi_sta();

  bool const use_ip =
      ae::WifiProbeProfileUsesCachedIp(profile) && g_cache.ip != 0;
  bool const use_ch =
      ae::WifiProbeProfileUsesChannel(profile) && g_cache.channel != 0;

  if (use_ip) {
    esp_netif_dhcpc_stop(g_netif);
    esp_netif_ip_info_t ip{};
    ip.ip.addr = g_cache.ip;
    ip.netmask.addr = g_cache.netmask;
    ip.gw.addr = g_cache.gateway;
    esp_netif_set_ip_info(g_netif, &ip);
  }

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  cfg.ampdu_rx_enable = 0;
  cfg.ampdu_tx_enable = 0;
  esp_wifi_init(&cfg);
  esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &OnWifi, nullptr);
  esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &OnWifi, nullptr);

  wifi_config_t wc{};
  std::strncpy(reinterpret_cast<char*>(wc.sta.ssid), WIFI_SSID,
               sizeof(wc.sta.ssid));
  std::strncpy(reinterpret_cast<char*>(wc.sta.password), WIFI_PASSWORD,
               sizeof(wc.sta.password));
  wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  if (use_ch) {
    wc.sta.channel = g_cache.channel;
  }
  // NO BSSID pinning.

  auto const t0 = esp_timer_get_time();
  esp_wifi_set_mode(WIFI_MODE_STA);
  esp_wifi_set_config(WIFI_IF_STA, &wc);
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_start();

  EventBits_t bits = xEventGroupWaitBits(g_eg, kWifiConnectedBit | kWifiFailBit,
                                         pdTRUE, pdFALSE, pdMS_TO_TICKS(20000));
  *connect_us = esp_timer_get_time() - t0;
  if ((bits & kWifiConnectedBit) == 0) {
    return false;
  }

  if (pre_ms > 0) {
    vTaskDelay(pdMS_TO_TICKS(pre_ms));
  }

  esp_netif_ip_info_t ipi{};
  if (esp_netif_get_ip_info(g_netif, &ipi) == ESP_OK && ipi.gw.addr != 0) {
    *gw_be = ipi.gw.addr;
  } else if (use_ip) {
    *gw_be = g_cache.gateway;
  }
  return *gw_be != 0;
}

void CaptureCanonicalCache() {
  wifi_ap_record_t ap{};
  esp_netif_ip_info_t ipi{};
  if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
    return;
  }
  if (esp_netif_get_ip_info(g_netif, &ipi) != ESP_OK) {
    return;
  }
  g_cache = {};
  g_cache.channel = ap.primary;
  g_cache.ip = ipi.ip.addr;
  g_cache.netmask = ipi.netmask.addr;
  g_cache.gateway = ipi.gw.addr;
  g_cache.authmode = static_cast<std::uint8_t>(ap.authmode);
  std::memcpy(g_cache.bssid, ap.bssid, 6);
  g_cache.flags = 1u | 2u;
  g_probe = {};
  g_probe.magic = ae::WifiProbeRtcState::kMagic;
  g_probe.version = ae::WifiProbeRtcState::kVersion;
  g_probe.ssid_hash = ae::WifiProbeHashSsid(WIFI_SSID);
  g_probe.channel = ap.primary;
  g_probe.ip = ipi.ip.addr;
  g_probe.netmask = ipi.netmask.addr;
  g_probe.gateway = ipi.gw.addr;
  g_probe.authmode = static_cast<std::uint8_t>(ap.authmode);
  std::memcpy(g_probe.bssid, ap.bssid, 6);
  ae::WifiProbeSealCrc(g_probe);
  std::printf("A_CACHE ssid=%s ch=%u ip=%08lx gw=%08lx auth=%u\n", WIFI_SSID,
              static_cast<unsigned>(g_cache.channel),
              static_cast<unsigned long>(g_cache.ip),
              static_cast<unsigned long>(g_cache.gateway),
              static_cast<unsigned>(g_cache.authmode));
  std::fflush(stdout);
}

std::uint32_t MedianMs(std::array<std::uint32_t, 64> const& s, int n) {
  if (n <= 0) {
    return 0;
  }
  auto tmp = s;
  for (int a = 1; a < n; ++a) {
    auto v = tmp[static_cast<std::size_t>(a)];
    int b = a;
    while (b > 0 && tmp[static_cast<std::size_t>(b - 1)] > v) {
      tmp[static_cast<std::size_t>(b)] = tmp[static_cast<std::size_t>(b - 1)];
      --b;
    }
    tmp[static_cast<std::size_t>(b)] = v;
  }
  return tmp[static_cast<std::size_t>(n / 2)];
}

std::uint32_t P90Ms(std::array<std::uint32_t, 64> const& s, int n) {
  if (n <= 0) {
    return 0;
  }
  auto tmp = s;
  for (int a = 1; a < n; ++a) {
    auto v = tmp[static_cast<std::size_t>(a)];
    int b = a;
    while (b > 0 && tmp[static_cast<std::size_t>(b - 1)] > v) {
      tmp[static_cast<std::size_t>(b)] = tmp[static_cast<std::size_t>(b - 1)];
      --b;
    }
    tmp[static_cast<std::size_t>(b)] = v;
  }
  int idx = (n * 9) / 10;
  if (idx >= n) {
    idx = n - 1;
  }
  return tmp[static_cast<std::size_t>(idx)];
}

RunStats RunMatrix(ae::WifiProbeProfile profile, std::uint16_t pre_ms,
                   int reconnects) {
  RunStats st{};
  for (int i = 0; i < reconnects; ++i) {
    std::int64_t c_us = 0;
    std::uint32_t gw = 0;
    bool ok = ConnectProfile(profile, pre_ms, &c_us, &gw);
    if (!ok) {
      ++st.fail;
      std::printf(
          "A_RES profile=%d pre=%u attempt=%d connect=0 ready=0 icmp_s=0 "
          "icmp_r=0 c_ms=-1\n",
          static_cast<int>(profile), static_cast<unsigned>(pre_ms), i + 1);
      std::fflush(stdout);
      Teardown();
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }
    ++st.connect_ok;
    ++st.ready_ok;
    if (st.n_samples < static_cast<int>(st.connect_ms.size())) {
      st.connect_ms[static_cast<std::size_t>(st.n_samples++)] =
          static_cast<std::uint32_t>(c_us / 1000);
    }
    auto icmp = ae::WifiGatewayIcmpProbe(gw, AE_PROBE_ICMP_PER);
    st.icmp_sent += icmp.stats.sent;
    st.icmp_recv += icmp.stats.received;
    std::printf(
        "A_RES profile=%d pre=%u attempt=%d connect=1 ready=1 icmp_s=%u "
        "icmp_r=%u c_ms=%ld\n",
        static_cast<int>(profile), static_cast<unsigned>(pre_ms), i + 1,
        static_cast<unsigned>(icmp.stats.sent),
        static_cast<unsigned>(icmp.stats.received),
        static_cast<long>(c_us / 1000));
    std::fflush(stdout);
    Teardown();
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  return st;
}

double LossPct(RunStats const& st) {
  if (st.icmp_sent <= 0) {
    return 100.0;
  }
  return 100.0 * static_cast<double>(st.icmp_sent - st.icmp_recv) /
         static_cast<double>(st.icmp_sent);
}

bool Passes(RunStats const& st) {
  if (st.fail > 0) {
    return false;
  }
  if (st.icmp_sent <= 0) {
    return false;
  }
  double recv_pct =
      100.0 * static_cast<double>(st.icmp_recv) / static_cast<double>(st.icmp_sent);
  return recv_pct + 1e-9 >= static_cast<double>(AE_PROBE_PASS_PCT);
}

void PrintSum(char const* tag, ae::WifiProbeProfile profile,
              std::uint16_t pre_ms, RunStats const& st) {
  std::printf(
      "%s profile=%d pre=%u connect_ok=%d ready_ok=%d icmp_sent=%d "
      "icmp_recv=%d icmp_loss=%.2f fail=%d connect_median_ms=%u "
      "connect_p90_ms=%u connect_max_ms=%u\n",
      tag, static_cast<int>(profile), static_cast<unsigned>(pre_ms),
      st.connect_ok, st.ready_ok, st.icmp_sent, st.icmp_recv, LossPct(st),
      st.fail, static_cast<unsigned>(MedianMs(st.connect_ms, st.n_samples)),
      static_cast<unsigned>(P90Ms(st.connect_ms, st.n_samples)),
      st.n_samples > 0
          ? static_cast<unsigned>([&] {
              std::uint32_t m = 0;
              for (int i = 0; i < st.n_samples; ++i) {
                if (st.connect_ms[static_cast<std::size_t>(i)] > m) {
                  m = st.connect_ms[static_cast<std::size_t>(i)];
                }
              }
              return m;
            }())
          : 0u);
  std::fflush(stdout);
}

}  // namespace

void setup() {
  std::printf("A_BEGIN ssid=%s reconnects=%d icmp_per=%d pass_pct=%d\n",
              WIFI_SSID, AE_PROBE_RECONNECTS, AE_PROBE_ICMP_PER,
              AE_PROBE_PASS_PCT);
  std::fflush(stdout);

  // Seed cache with canonical P0.
  {
    std::int64_t c = 0;
    std::uint32_t gw = 0;
    if (ConnectProfile(ae::WifiProbeProfile::kP0Default, 100, &c, &gw)) {
      CaptureCanonicalCache();
    } else {
      std::printf("A_ERROR seed_connect_failed\n");
      std::fflush(stdout);
    }
    Teardown();
    vTaskDelay(pdMS_TO_TICKS(500));
  }

  std::array<std::uint16_t, 5> min_pre{};
  std::array<std::uint8_t, 5> baseline_ok{};
  std::array<std::uint32_t, 5> med_ms{};
  std::array<double, 5> loss{};
  min_pre.fill(0xffff);
  baseline_ok.fill(0);

  constexpr std::uint16_t kBasePre = 300;
  for (std::uint8_t pi = 0;
       pi < static_cast<std::uint8_t>(ae::WifiProbeProfile::kCount); ++pi) {
    auto const profile = static_cast<ae::WifiProbeProfile>(pi);
    auto st = RunMatrix(profile, kBasePre, AE_PROBE_RECONNECTS);
    PrintSum("A_SUM", profile, kBasePre, st);
    loss[pi] = LossPct(st);
    med_ms[pi] = MedianMs(st.connect_ms, st.n_samples);
    if (Passes(st)) {
      baseline_ok[pi] = 1;
      min_pre[pi] = kBasePre;
      // PRE search downward; stop on clear FAIL.
      static constexpr std::uint16_t kPres[] = {300, 200, 100, 50, 25, 10, 0};
      for (auto pre : kPres) {
        if (pre >= kBasePre) {
          continue;  // already measured at 300
        }
        auto pst = RunMatrix(profile, pre, AE_PROBE_PRE_RECONNECTS);
        PrintSum("A_PRE", profile, pre, pst);
        if (!Passes(pst)) {
          std::printf("A_PRE_STOP profile=%d fail_at_pre=%u last_stable=%u\n",
                      static_cast<int>(profile), static_cast<unsigned>(pre),
                      static_cast<unsigned>(min_pre[pi]));
          std::fflush(stdout);
          break;
        }
        min_pre[pi] = pre;
      }
    } else {
      std::printf("A_BASE_FAIL profile=%d\n", static_cast<int>(profile));
      std::fflush(stdout);
    }
  }

  // Reliability-first winner: highest profile that passed, then lowest PRE,
  // then lowest connect median.
  int winner = -1;
  for (int pi = 0; pi < 5; ++pi) {
    if (!baseline_ok[static_cast<std::size_t>(pi)]) {
      continue;
    }
    if (winner < 0) {
      winner = pi;
      continue;
    }
    auto const wi = static_cast<std::size_t>(winner);
    auto const ci = static_cast<std::size_t>(pi);
    if (loss[ci] < loss[wi] - 0.01) {
      winner = pi;
    } else if (loss[ci] <= loss[wi] + 0.01) {
      if (min_pre[ci] < min_pre[wi]) {
        winner = pi;
      } else if (min_pre[ci] == min_pre[wi] && med_ms[ci] < med_ms[wi]) {
        winner = pi;
      } else if (min_pre[ci] == min_pre[wi] && med_ms[ci] == med_ms[wi] &&
                 pi > winner) {
        // Prefer richer cache when equal reliability/speed.
        winner = pi;
      }
    }
  }

  if (winner >= 0) {
    g_probe.selected_profile = static_cast<std::uint8_t>(winner);
    g_probe.valid_profiles_bitmap = 0;
    for (int i = 0; i < 5; ++i) {
      if (baseline_ok[static_cast<std::size_t>(i)]) {
        g_probe.valid_profiles_bitmap |=
            static_cast<std::uint8_t>(1u << i);
      }
    }
    g_probe.pre_send_delay_ms = min_pre[static_cast<std::size_t>(winner)];
    g_probe.post_send_delay_ms = 300;
    ae::WifiProbeSealCrc(g_probe);
    std::printf(
        "A_WINNER profile=%d pre_ms=%u loss=%.2f connect_median_ms=%u "
        "valid_bitmap=0x%02x\n",
        winner, static_cast<unsigned>(g_probe.pre_send_delay_ms),
        loss[static_cast<std::size_t>(winner)],
        static_cast<unsigned>(med_ms[static_cast<std::size_t>(winner)]),
        static_cast<unsigned>(g_probe.valid_profiles_bitmap));
  } else {
    std::printf("A_WINNER profile=-1 pre_ms=300 loss=100\n");
  }
  std::printf("A_DONE\n");
  std::fflush(stdout);
}

void loop() { vTaskDelay(pdMS_TO_TICKS(60000)); }
