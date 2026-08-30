/*
 * Copyright 2026 Aethernet Inc.
 *
 * No-sleep reliability E2E on AP aethernetio (ESP32-C6).
 * 5 FULL x 5 HOT prepared reconnects. PRE=300 POST=300. No deep/light sleep.
 * D1 WIFI_STORAGE_RAM kept. Fallbacks: channel → scan, static IP → DHCP,
 * static ARP → ARP wait. Silent UART; telemetry via Æther NosleepPayload 0xD9.
 */

#include <cstdint>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "bench_payload.h"
#include "prepared_send/prepared_send.h"

#if defined(ESP_PLATFORM)
#  include <esp_event.h>
#  include <esp_mac.h>
#  include <esp_netif.h>
#  include <esp_timer.h>
#  include <esp_wifi.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#endif

using namespace std::chrono_literals;

namespace temp_sensor {
namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");

#ifndef BENCH_CLIENT_ID
#  define BENCH_CLIENT_ID "prepared_deepsleep_5x50_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("5aade50f-00d9-4624-b097-e203cdcf1e38");
#endif

static constexpr std::uint8_t kOuterCycles = 5;
static constexpr std::uint8_t kHotPerOuter = 5;
static constexpr std::uint16_t kPreMs = 300;
static constexpr std::uint16_t kPostMs = 300;
static constexpr std::uint16_t kCbTimeoutMs = 300;
static constexpr std::uint32_t kHotGapMs = 1000;

#if defined(ESP_PLATFORM)

static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

static bool g_had_aether_app = false;
static std::shared_ptr<ae::AetherApp> g_app;
static ae::Client::ptr g_client;
static std::unique_ptr<ae::P2pStream> g_stream;
static ae::Subscription g_select_sub;
static ae::Subscription g_stream_sub;
static ae::Subscription g_write_sub;

static bool g_write_armed = false;
static bool g_write_ok = false;
static bool g_exit_success = false;
static bool g_pending_register_finish = false;
static bool g_pending_full_post_write = false;
static bool g_pending_final_exit = false;
static bool g_done = false;
static bool g_registered = false;

static std::uint8_t g_outer = 1;
static std::uint8_t g_hot = 1;
static std::uint16_t g_seq = 0;
static std::uint16_t g_record_id = 1;
static bool g_need_final = false;
static bool g_need_hots = false;

static prepared_send::BisectWifiCacheSnapshot g_wifi_cache{};
static prepared_send::FastPathConfig g_cfg{};

static bench::NosleepPayload g_pending_fail{};
static bool g_pending_fail_valid = false;

static char g_full_ssid[33]{};
static std::uint8_t g_full_bssid[6]{};
static std::uint8_t g_sta_mac[6]{};
static std::uint8_t g_full_channel = 0;
static std::int8_t g_full_rssi = 0;
static std::uint8_t g_full_auth = 0;
static std::uint32_t g_full_ip = 0;
static std::uint32_t g_full_netmask = 0;
static std::uint32_t g_full_gateway = 0;

static std::uint16_t NextSeq() {
  ++g_seq;
  return g_seq;
}

static void CaptureFullApIdentity() {
  std::memset(g_full_ssid, 0, sizeof(g_full_ssid));
  std::memset(g_full_bssid, 0, sizeof(g_full_bssid));
  g_full_channel = 0;
  g_full_rssi = 0;
  g_full_auth = 0;
  g_full_ip = 0;
  g_full_netmask = 0;
  g_full_gateway = 0;

  wifi_ap_record_t ap{};
  if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
    std::memcpy(g_full_ssid, ap.ssid,
                sizeof(g_full_ssid) - 1 < sizeof(ap.ssid)
                    ? sizeof(g_full_ssid) - 1
                    : sizeof(ap.ssid));
    std::memcpy(g_full_bssid, ap.bssid, sizeof(g_full_bssid));
    g_full_channel = ap.primary;
    g_full_rssi = ap.rssi;
    g_full_auth = static_cast<std::uint8_t>(ap.authmode);
  }
  (void)esp_wifi_get_mac(WIFI_IF_STA, g_sta_mac);

  esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if (netif != nullptr) {
    esp_netif_ip_info_t ip{};
    if (esp_netif_get_ip_info(netif, &ip) == ESP_OK) {
      g_full_ip = ip.ip.addr;
      g_full_netmask = ip.netmask.addr;
      g_full_gateway = ip.gw.addr;
    }
  }
}

static void FillIdentity(bench::NosleepPayload& p) {
  std::memcpy(p.ssid, g_full_ssid, sizeof(p.ssid));
  std::memcpy(p.bssid, g_full_bssid, sizeof(p.bssid));
  std::memcpy(p.sta_mac, g_sta_mac, sizeof(p.sta_mac));
  p.channel = g_full_channel;
  p.rssi = g_full_rssi;
  p.authmode = g_full_auth;
  p.ip = g_full_ip;
  p.netmask = g_full_netmask;
  p.gateway = g_full_gateway;
}

static void FillHotResult(bench::NosleepPayload& p,
                          prepared_send::FastSendResult const& r) {
  p.wifi_init_us = r.wifi_init_us;
  p.connect_us = r.connect_us;
  p.tx_done_us = r.tx_done_wait_us;
  p.teardown_us = r.teardown_us;
  p.hot_total_us = r.cycle_us;
  p.pre_delay_ms = kPreMs;
  p.post_delay_ms = kPostMs;
  p.cb_seen = r.cb_any;
  p.cb_timeout = r.cb_timeout;
  p.first_status = r.first_status;
  p.sendto_ok = r.sendto_ok;
  p.fail_stage = r.fail_stage;
  p.disc_reason = r.last_disconnect_reason;
  p.disc_count = r.disconnect_count;
  p.reconnect_count = r.reconnect_count;
  std::memcpy(p.hot_bssid, r.bssid, sizeof(p.hot_bssid));
  p.hot_rssi = r.rssi;
  p.hot_channel = r.actual_channel;
  p.hot_auth = r.negotiated_auth;

  std::uint8_t f = 0;
  if (r.sta_connected_seen) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kStaConnected);
  }
  if (r.got_ip_seen) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kGotIp);
  }
  if (r.used_cached_channel) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kUsedCachedChannel);
  }
  if (r.channel_fallback_used) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kChannelFallback);
  }
  if (r.used_static_ip) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kUsedStaticIp);
  }
  if (r.dhcp_fallback_used) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kDhcpFallback);
  }
  if (r.used_static_arp) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kUsedStaticArp);
  }
  if (r.arp_fallback_used) {
    f |= static_cast<std::uint8_t>(bench::NosleepFlags::kArpFallback);
  }
  p.flags = f;
}

static prepared_send::FastSendResult g_pending_hot_meas{};
static bool g_pending_hot_meas_valid = false;
static std::uint8_t g_pending_hot_meas_index = 0;

static ae::DataBuffer MakeFullPayload() {
  bench::NosleepPayload p{};
  p.type = static_cast<std::uint8_t>(bench::NosleepMsgType::kFull);
  p.outer_cycle = g_outer;
  p.hot_index = 0;
  p.sequence_global = NextSeq();
  p.record_id = g_record_id++;
  FillIdentity(p);
  p.pre_delay_ms = kPreMs;
  p.post_delay_ms = kPostMs;
  if (g_pending_hot_meas_valid) {
    FillHotResult(p, g_pending_hot_meas);
    p.hot_index = g_pending_hot_meas_index;
    g_pending_hot_meas_valid = false;
  }
  if (g_pending_fail_valid) {
    p.pending_fail_valid = 1;
    if (p.fail_stage == 0) {
      p.fail_stage = g_pending_fail.fail_stage;
      p.disc_reason = g_pending_fail.disc_reason;
      p.disc_count = g_pending_fail.disc_count;
    }
    g_pending_fail_valid = false;
  }
  return bench::EncodeNosleep<ae::DataBuffer>(p);
}

static ae::DataBuffer MakeHotSendPayload(std::uint8_t sending_hot) {
  bench::NosleepPayload p{};
  p.type = static_cast<std::uint8_t>(bench::NosleepMsgType::kHot);
  p.outer_cycle = g_outer;
  p.sequence_global = NextSeq();
  p.record_id = g_record_id++;
  FillIdentity(p);
  p.pre_delay_ms = kPreMs;
  p.post_delay_ms = kPostMs;
  // Flush prior successful HOT measurement (pending pattern).
  if (g_pending_hot_meas_valid) {
    FillHotResult(p, g_pending_hot_meas);
    p.hot_index = g_pending_hot_meas_index;
    g_pending_hot_meas_valid = false;
  } else {
    p.hot_index = sending_hot;  // first packet: identity only
  }
  if (g_pending_fail_valid) {
    p.pending_fail_valid = 1;
    g_pending_fail_valid = false;
  }
  return bench::EncodeNosleep<ae::DataBuffer>(p);
}

static ae::DataBuffer MakeFinalPayload() {
  bench::NosleepPayload p{};
  p.type = static_cast<std::uint8_t>(bench::NosleepMsgType::kFinal);
  p.outer_cycle = g_outer;
  p.sequence_global = NextSeq();
  p.record_id = g_record_id++;
  FillIdentity(p);
  if (g_pending_hot_meas_valid) {
    FillHotResult(p, g_pending_hot_meas);
    p.hot_index = g_pending_hot_meas_index;
    g_pending_hot_meas_valid = false;
  } else {
    p.hot_index = kHotPerOuter;
  }
  if (g_pending_fail_valid) {
    p.pending_fail_valid = 1;
    g_pending_fail_valid = false;
  }
  return bench::EncodeNosleep<ae::DataBuffer>(p);
}

static void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
  g_app.reset();
}

static void PreConstructCleanup() {
  if (!g_had_aether_app) {
    return;
  }
#  if !AE_WIFI_USE_FULL_DEINIT
  esp_netif_deinit();
  esp_event_loop_delete_default();
#  endif
}

static void ConstructAether() {
  PreConstructCleanup();
  g_had_aether_app = true;
  g_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}
#  if AE_DISTILLATION
          .AddAdapterFactory([&](ae::AetherAppContext const& ctx) {
            return ae::WifiAdapter::ptr::Create(
                ae::CreateWith{ctx.domain()}.with_id(
                    ae::GlobalId::kWiFiAdapter),
                ctx.aether(), ctx.poller(), ctx.dns_resolver(), kWifiInit);
          })
#  endif
  );
}

static prepared_send::FastPathConfig MakeCfg() {
  prepared_send::FastPathConfig c{};
  c.use_bssid = false;
  c.use_channel = true;
  c.use_fast_scan = false;
  c.use_static_ip = true;
  c.use_static_arp = true;
  c.arp_wait_on_miss = true;
  c.wifi_storage_ram = true;
  c.wifi_nvs_enable = true;
  c.auth = prepared_send::FastAuthMode::kWpa2;
  c.retry_max = 10;
  c.pre_delay_ms = kPreMs;
  c.post_delay_ms = kPostMs;
  c.post_mode = prepared_send::FastPostMode::kTxDoneCb;
  c.tx_done_timeout_ms = kCbTimeoutMs;
  return c;
}

static void DoFullWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  CaptureFullApIdentity();
  auto& wa = g_stream->Write(MakeFullPayload());
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    g_write_ok = (st == ae::WriteAction::Status::kSuccess);
    g_pending_full_post_write = true;
  });
}

static void MaybeFullWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  DoFullWrite();
}

static void OnFullClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  auto client = g_client.Load();
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             ae::P2pPortHandle{});
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeFullWrite(); });
  MaybeFullWrite();
}

static void StartRegister() {
  g_write_armed = false;
  g_pending_register_finish = false;
  g_exit_success = false;
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       g_client = std::move(res).value();
                       g_pending_register_finish = true;
                     });
}

static void StartFull() {
  g_write_armed = false;
  g_pending_full_post_write = false;
  g_write_ok = false;
  g_exit_success = false;
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       OnFullClientReady(std::move(res).value());
                     });
}

static void DoFinalWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  CaptureFullApIdentity();
  auto& wa = g_stream->Write(MakeFinalPayload());
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    g_write_ok = (st == ae::WriteAction::Status::kSuccess);
    g_pending_final_exit = true;
  });
}

static void MaybeFinalWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  DoFinalWrite();
}

static void OnFinalClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  auto client = g_client.Load();
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             ae::P2pPortHandle{});
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeFinalWrite(); });
  MaybeFinalWrite();
}

static void StartFinal() {
  g_write_armed = false;
  g_pending_final_exit = false;
  g_write_ok = false;
  g_exit_success = false;
  ConstructAether();
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       if (!res) {
                         g_app->Exit(1);
                         return;
                       }
                       OnFinalClientReady(std::move(res).value());
                     });
}

static void FinishRegisterInLoop() {
  g_app->aether().Save();
  g_exit_success = true;
  g_app->Exit(0);
}

static void FinishFullPostWriteInLoop() {
  if (!g_write_ok) {
    g_app->Exit(1);
    return;
  }
  CaptureFullApIdentity();
  if (!prepared_send::FreezeBisectWifiCacheFromActiveConnection()) {
    g_app->Exit(1);
    return;
  }
  g_wifi_cache = prepared_send::GetBisectWifiCacheSnapshot();
  // Never persist BSSID for HOT path.
  g_wifi_cache.valid_bssid = false;
  std::memset(g_wifi_cache.bssid, 0, sizeof(g_wifi_cache.bssid));

  if (!prepared_send::ExportPreparedSendBlock(g_client, kServiceUid,
                                              kHotPerOuter)) {
    g_app->Exit(1);
    return;
  }
  if (!prepared_send::HasPreparedSendBlock() ||
      prepared_send::PreparedMessageLeft() !=
          static_cast<std::uint32_t>(kHotPerOuter)) {
    g_app->Exit(1);
    return;
  }
  g_app->aether().Save();
  g_exit_success = true;
  g_app->Exit(0);
}

static void FinishFinalInLoop() {
  if (!g_write_ok) {
    g_app->Exit(1);
    return;
  }
  g_exit_success = true;
  g_app->Exit(0);
}

static void RunHotBlock() {
  for (std::uint8_t hi = 1; hi <= kHotPerOuter; ++hi) {
    g_hot = hi;
    bool sent = false;
    for (int attempt = 0; attempt < 3 && !sent; ++attempt) {
      auto payload = MakeHotSendPayload(hi);
      auto result = prepared_send::SendPreparedOnceReliability(
          g_cfg, payload, &g_wifi_cache);

      if (result.status == prepared_send::HotSendStatus::kSent) {
        g_pending_hot_meas = result;
        g_pending_hot_meas_index = hi;
        g_pending_hot_meas_valid = true;
        sent = true;
        break;
      }

      g_pending_fail = {};
      g_pending_fail.type =
          static_cast<std::uint8_t>(bench::NosleepMsgType::kHotFail);
      g_pending_fail.outer_cycle = g_outer;
      g_pending_fail.hot_index = hi;
      FillHotResult(g_pending_fail, result);
      g_pending_fail_valid = true;

      bool const before_encode =
          result.status == prepared_send::HotSendStatus::kWifiFailed ||
          (result.status_flags &
           static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk)) == 0;
      if (!before_encode) {
        break;  // nonce may be consumed; do not spin
      }
      vTaskDelay(pdMS_TO_TICKS(kHotGapMs));
    }

    if (hi < kHotPerOuter) {
      vTaskDelay(pdMS_TO_TICKS(kHotGapMs));
    }
  }
}

#endif  // ESP_PLATFORM

}  // namespace
}  // namespace temp_sensor

#if defined(ESP_PLATFORM)

void setup() {
  using namespace temp_sensor;
  nvs_flash_init();
  g_cfg = MakeCfg();
  g_done = false;
  g_outer = 1;
  g_hot = 1;
  g_need_hots = false;
  g_need_final = false;
  prepared_send::InvalidatePreparedWifiCache();
  g_wifi_cache = {};
  StartRegister();
}

void loop() {
  using namespace temp_sensor;
  if (g_done) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    return;
  }

  auto process_deferred = []() {
    if (g_app && g_pending_register_finish) {
      g_pending_register_finish = false;
      FinishRegisterInLoop();
      return true;
    }
    if (g_app && g_pending_full_post_write) {
      g_pending_full_post_write = false;
      FinishFullPostWriteInLoop();
      return true;
    }
    if (g_app && g_pending_final_exit) {
      g_pending_final_exit = false;
      FinishFinalInLoop();
      return true;
    }
    return false;
  };

  if (g_need_hots) {
    g_need_hots = false;
    RunHotBlock();
    if (g_outer < kOuterCycles) {
      ++g_outer;
      StartFull();
    } else {
      g_need_final = true;
      StartFinal();
    }
    return;
  }

  if (process_deferred()) {
    return;
  }

  if (!g_app) {
    if (!g_registered) {
      StartRegister();
    } else if (g_need_final) {
      StartFinal();
    } else {
      StartFull();
    }
    return;
  }

  if (!g_app->IsExited()) {
    auto t = g_app->Update(ae::Now());
    if (process_deferred()) {
      return;
    }
    if (!g_app->IsExited()) {
      g_app->WaitUntil(t);
    }
    return;
  }

  // App exited.
  if (!g_registered) {
    if (g_exit_success) {
      ReleaseApp();
      g_registered = true;
      g_outer = 1;
      StartFull();
    } else {
      ReleaseApp();
      vTaskDelay(pdMS_TO_TICKS(1000));
      StartRegister();
    }
    return;
  }

  if (g_need_final) {
    ReleaseApp();
    g_done = true;
    return;
  }

  // FULL completed.
  if (g_exit_success) {
    ReleaseApp();
    prepared_send::ReleaseFullAetherWifiForHotPath();
    g_need_hots = true;
  } else {
    ReleaseApp();
    vTaskDelay(pdMS_TO_TICKS(1000));
    StartFull();
  }
}

#else

void setup() {}
void loop() {}

#endif
