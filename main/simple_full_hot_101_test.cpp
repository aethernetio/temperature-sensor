/*
 * AE_EXP_SIMPLE_FULL_HOT_101 — 1× FULL + 100× HOT (1-byte MSG_NUM), FS_INIT UDP Æther.
 */
#include <memory>

#include "aether/adapters/wifi_adapter.h"
#include "aether/all.h"
#include "aether/config.h"
#include "aether/prepared_packet/prepared_send_message.h"
#include "aether/prepared_packet/packet_encoder.h"

#include <cstring>

#include <esp_event.h>
#include <esp_netif.h>
#include <esp_netif_net_stack.h>
#include <esp_private/wifi.h>
#include <esp_sleep.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <lwip/etharp.h>
#include <lwip/ip_addr.h>
#include <lwip/netif.h>
#include <lwip/pbuf.h>
#include <lwip/tcpip.h>
#include <lwip/udp.h>
#include <nvs_flash.h>

extern "C" esp_err_t esp_wifi_internal_set_retry_counter(uint8_t short_retry,
                                                         uint8_t long_retry);

namespace temp_sensor {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
static constexpr int kHotCount = 100;
static constexpr std::uint64_t kSleepUs = 2'000'000;
static constexpr int kHotPostSendHoldMs = 1000;
static constexpr int kHotPreSettleMs = 50;

struct WifiRtcCache {
  std::uint8_t channel;
  std::uint8_t bssid[6];
  std::uint8_t gw_mac[6];
  std::uint32_t ip;
  std::uint32_t netmask;
  std::uint32_t gateway;
};

RTC_DATA_ATTR static std::uint8_t g_lane = 1;
RTC_DATA_ATTR static WifiRtcCache g_wifi_cache;
RTC_NOINIT_ATTR static ae::prepared_packet::PreparedSendMessageBlock g_prep_block;

static std::shared_ptr<ae::AetherApp> g_app;
static ae::Client::ptr g_client;
static std::unique_ptr<ae::P2pStream> g_stream;
static ae::Subscription g_select_sub;
static ae::Subscription g_stream_sub;
static ae::Subscription g_write_sub;
static bool g_write_armed;

static constexpr int kHotGotIpBit = BIT0;
static EventGroupHandle_t g_hot_events;
static bool g_hot_want_reconnect;

static void HotOnWifiEvent(void*, esp_event_base_t base, std::int32_t id, void*) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START && g_hot_want_reconnect) {
    esp_wifi_connect();
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED &&
             g_hot_want_reconnect) {
    esp_wifi_connect();
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    xEventGroupSetBits(g_hot_events, kHotGotIpBit);
  }
}

void AppSetup() {
  nvs_flash_init();
  esp_event_loop_delete_default();

  if (g_lane != 1) {
    ae::DataBuffer body{g_lane};
    g_lane++;

    ae::DataBuffer packet;
    ae::prepared_packet::EncodePacket(g_prep_block, body, packet);

    auto const endpoint = g_prep_block.Resolve()->endpoint;
    std::uint32_t dest_ip;
    std::memcpy(&dest_ip, &endpoint.address.Get<ae::IpV4Addr>().ipv4_value, 4);
    std::uint16_t const dest_port = endpoint.port;

    g_hot_want_reconnect = true;
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_t* netif = esp_netif_create_default_wifi_sta();
    g_hot_events = xEventGroupCreate();

    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&wcfg);
    esp_wifi_set_storage(WIFI_STORAGE_RAM);
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &HotOnWifiEvent,
                               nullptr);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &HotOnWifiEvent,
                               nullptr);

    esp_netif_dhcpc_stop(netif);
    esp_netif_ip_info_t ip_info;
    ip_info.ip.addr = g_wifi_cache.ip;
    ip_info.netmask.addr = g_wifi_cache.netmask;
    ip_info.gw.addr = g_wifi_cache.gateway;
    esp_netif_set_ip_info(netif, &ip_info);

    wifi_config_t wifi;
    std::memset(&wifi, 0, sizeof(wifi));
    std::strncpy(reinterpret_cast<char*>(wifi.sta.ssid), WIFI_SSID,
                 sizeof(wifi.sta.ssid));
    std::strncpy(reinterpret_cast<char*>(wifi.sta.password), WIFI_PASSWORD,
                 sizeof(wifi.sta.password));
    wifi.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi.sta.scan_method = WIFI_FAST_SCAN;
    wifi.sta.channel = g_wifi_cache.channel;
    wifi.sta.bssid_set = true;
    std::memcpy(wifi.sta.bssid, g_wifi_cache.bssid, sizeof(wifi.sta.bssid));

    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wifi);
    esp_wifi_set_protocol(WIFI_IF_STA,
                          WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
    esp_wifi_start();
    esp_wifi_set_ps(WIFI_PS_NONE);

    xEventGroupWaitBits(g_hot_events, kHotGotIpBit, pdFALSE, pdFALSE,
                        pdMS_TO_TICKS(10000));
    g_hot_want_reconnect = false;
    esp_wifi_set_ps(WIFI_PS_MAX_MODEM);

    ip4_addr_t gw_ip;
    gw_ip.addr = g_wifi_cache.gateway;
    struct eth_addr gw_eth;
    std::memcpy(gw_eth.addr, g_wifi_cache.gw_mac, 6);
    struct netif* lwip_netif =
        static_cast<struct netif*>(esp_netif_get_netif_impl(netif));

    LOCK_TCPIP_CORE();
    etharp_add_static_entry(&gw_ip, &gw_eth);
    etharp_gratuitous(lwip_netif);
    UNLOCK_TCPIP_CORE();

    vTaskDelay(pdMS_TO_TICKS(kHotPreSettleMs));
    esp_wifi_internal_set_fix_rate(WIFI_IF_STA, true, WIFI_PHY_RATE_1M_L);
    esp_wifi_internal_set_retry_counter(3, 3);

    ip_addr_t dest;
    ip_addr_set_ip4_u32(&dest, dest_ip);

    LOCK_TCPIP_CORE();
    struct udp_pcb* upcb = udp_new();
    struct pbuf* p = pbuf_alloc(PBUF_TRANSPORT, static_cast<u16_t>(packet.size()),
                                PBUF_RAM);
    std::memcpy(p->payload, packet.data(), packet.size());
    udp_sendto(upcb, p, &dest, dest_port);
    pbuf_free(p);
    udp_remove(upcb);
    UNLOCK_TCPIP_CORE();

    vTaskDelay(pdMS_TO_TICKS(kHotPostSendHoldMs));

    LOCK_TCPIP_CORE();
    etharp_remove_static_entry(&gw_ip);
    UNLOCK_TCPIP_CORE();

    esp_event_handler_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, &HotOnWifiEvent);
    esp_event_handler_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, &HotOnWifiEvent);
    esp_wifi_stop();
    esp_wifi_deinit();
    esp_netif_destroy_default_wifi(netif);
    vEventGroupDelete(g_hot_events);
    esp_event_loop_delete_default();
    esp_netif_deinit();

    esp_sleep_enable_timer_wakeup(kSleepUs);
    esp_deep_sleep_start();
  }

  g_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}.AdaptersFactory([](ae::AetherAppContext const& ctx) {
        auto ap = ae::AdapterRegistry::ptr{ctx.aether()->adapter_registry};
        auto loaded_reg = ap.Load();
        ae::WiFiInit init{
            std::vector<ae::WiFiAp>{
                {ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
            std::nullopt};
        for (auto const& adapter : loaded_reg->adapters()) {
          ae::WifiAdapter::ptr wa{adapter};
          wa.Load()->wifi_init_ = init;
        }
        return ap;
      }));

  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       g_client = std::move(res).value();
                       auto loaded = g_client.Load();
                       auto handle =
                           loaded->message_stream_manager().CreatePort(kServiceUid);
                       g_stream = std::make_unique<ae::P2pStream>(
                           *g_app, loaded, kServiceUid, std::move(handle));
                       auto full_write = [&]() {
                         if (g_write_armed || !g_stream->stream_info().is_writable) {
                           return;
                         }
                         g_write_armed = true;
                         ae::DataBuffer body{static_cast<std::uint8_t>(1)};
                         auto& wa = g_stream->Write(std::move(body));
                         g_write_sub = wa.status_event().Subscribe(
                             [](ae::WriteAction::Status st) {
                               if (st != ae::WriteAction::Status::kSuccess) {
                                 abort();
                               }

                               esp_netif_t* netif =
                                   esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
                               esp_netif_ip_info_t ip_info;
                               esp_netif_get_ip_info(netif, &ip_info);
                               wifi_ap_record_t ap;
                               esp_wifi_sta_get_ap_info(&ap);

                               WifiRtcCache c;
                               c.ip = ip_info.ip.addr;
                               c.netmask = ip_info.netmask.addr;
                               c.gateway = ip_info.gw.addr;
                               c.channel = ap.primary;
                               std::memcpy(c.bssid, ap.bssid, sizeof(c.bssid));

                               struct netif* lwip_netif = static_cast<struct netif*>(
                                   esp_netif_get_netif_impl(netif));
                               ip4_addr_t gw;
                               gw.addr = ip_info.gw.addr;
                               LOCK_TCPIP_CORE();
                               struct eth_addr* eth_ret;
                               ip4_addr_t const* ip_ret;
                               etharp_find_addr(lwip_netif, &gw, &eth_ret, &ip_ret);
                               std::memcpy(c.gw_mac, eth_ret->addr, 6);
                               UNLOCK_TCPIP_CORE();

                               g_wifi_cache = c;

                               g_prep_block =
                                   std::move(
                                       ae::prepared_packet::PrepareSendMessageBlock(
                                           g_client, kServiceUid, kHotCount))
                                       .value();

                               g_app->aether().Save();

                               esp_wifi_stop();
                               esp_wifi_deinit();
                               esp_netif_destroy(netif);
                               esp_netif_deinit();
                               esp_event_loop_delete_default();
                               vTaskDelay(pdMS_TO_TICKS(50));

                               g_lane = 2;
                               esp_sleep_enable_timer_wakeup(kSleepUs);
                               esp_deep_sleep_start();
                             });
                       };
                       g_stream_sub =
                           g_stream->stream_update_event().Subscribe(full_write);
                       full_write();
                     });
}

void AppLoop() { g_app->Update(ae::Now()); }

}  // namespace temp_sensor

extern "C" void ae_exp_on_wifi_start(void) { esp_wifi_set_ps(WIFI_PS_NONE); }

void setup() { temp_sensor::AppSetup(); }
void loop() { temp_sensor::AppLoop(); }
