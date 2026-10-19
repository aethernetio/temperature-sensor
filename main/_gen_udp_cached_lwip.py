from pathlib import Path

src = Path("udp_cached_lwip_send.cpp.bsource").read_text(encoding="utf-8")
if src.startswith("\ufeff"):
    src = src.lstrip("\ufeff")
start = src.find("namespace {")
end = src.find('extern "C" void RunUdpRtcIndexLoop')
body = src[start:end]
body = body.replace(
    "std::uint8_t payload[4]{}",
    "std::uint8_t const* payload_ptr{nullptr};\n  std::size_t payload_len{0};",
)
body = body.replace(
    "pbuf_alloc(PBUF_TRANSPORT, sizeof(c->payload), PBUF_RAM)",
    "pbuf_alloc(PBUF_TRANSPORT, static_cast<u16_t>(c->payload_len), PBUF_RAM)",
)
body = body.replace(
    "std::memcpy(p->payload, c->payload, sizeof(c->payload));",
    "if (c->payload_len > 0 && c->payload_ptr != nullptr) {\n"
    "    std::memcpy(p->payload, c->payload_ptr, c->payload_len);\n"
    "  }",
)
header = """/*
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

"""
footer = r"""

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
    send.dest_ip.addr = req.dest_ipv4;
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
"""
body = body.replace("namespace {", "namespace temp_sensor::udp_cached_lwip {\nnamespace {")
body = body + "\n}  // anonymous namespace\n"
Path("udp_cached_lwip_send.cpp").write_text(header + body + footer, encoding="utf-8")
print("wrote", Path("udp_cached_lwip_send.cpp").stat().st_size)
