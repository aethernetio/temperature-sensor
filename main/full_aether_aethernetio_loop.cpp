/*
 * Copyright 2026 Aethernet Inc.
 *
 * Canonical FULL Aether path baseline on AP aethernetio.
 * No prepared path, no deep sleep, no Wi-Fi cache / static IP / ARP.
 * Wi-Fi only via ae::WifiAdapter + EspWifiDriver.
 */

#include <cstdint>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "bench_payload.h"

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
#  define BENCH_CLIENT_ID "full_aether_aethernetio_baseline_v1"
#endif
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("5aade50f-00d9-4624-b097-e203cdcf1e38");
#endif

static constexpr std::uint32_t kGapMs = 1000;

#if defined(ESP_PLATFORM)

static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
    {},
};

enum class Phase : std::uint8_t { kRegister = 0, kFull = 1 };

static std::shared_ptr<ae::AetherApp> g_app;
static ae::Client::ptr g_client;
static std::unique_ptr<ae::P2pStream> g_stream;
static ae::Subscription g_select_sub;
static ae::Subscription g_stream_sub;
static ae::Subscription g_write_sub;

static Phase g_phase = Phase::kRegister;
static bool g_write_armed = false;
static bool g_write_ok = false;
static bool g_exit_ok = false;
static bool g_pending_register = false;
static bool g_pending_full_done = false;
static bool g_registered = false;
static bool g_had_app = false;

static std::uint32_t g_cycle = 0;
static std::uint32_t g_sequence = 0;

static std::int64_t g_t0 = 0;
static std::uint32_t g_construct_us = 0;
static std::uint32_t g_select_us = 0;
static std::uint32_t g_stream_us = 0;
static std::uint32_t g_writable_us = 0;
static std::uint32_t g_write_us = 0;
static std::uint32_t g_save_us = 0;
static std::uint32_t g_release_us = 0;
static std::uint32_t g_total_us = 0;

static std::int64_t SinceUs() { return esp_timer_get_time() - g_t0; }

static void ForceWifiRuntimeCleanup() {
  // After AetherApp release, ensure generic ESP-IDF Wi-Fi is down so the next
  // Construct starts from a clean driver state (same pattern as full-cycle
  // benches). Not used as a connection/hot path.
  (void)esp_wifi_disconnect();
  (void)esp_wifi_stop();
  (void)esp_wifi_deinit();
  (void)esp_netif_deinit();
  (void)esp_event_loop_delete_default();
}

static void CaptureApDiagnostics(bench::FullBaselinePayload& p) {
  std::memset(p.ssid, 0, sizeof(p.ssid));
  std::memset(p.bssid, 0, sizeof(p.bssid));
  p.channel = 0;
  p.rssi = 0;
  p.authmode = 0;
  p.ip = 0;
  p.netmask = 0;
  p.gateway = 0;

  wifi_ap_record_t ap{};
  if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
    std::memcpy(p.ssid, ap.ssid,
                sizeof(p.ssid) - 1 < sizeof(ap.ssid) ? sizeof(p.ssid) - 1
                                                    : sizeof(ap.ssid));
    std::memcpy(p.bssid, ap.bssid, sizeof(p.bssid));
    p.channel = ap.primary;
    p.rssi = ap.rssi;
    p.authmode = static_cast<std::uint8_t>(ap.authmode);
  }
  (void)esp_wifi_get_mac(WIFI_IF_STA, p.sta_mac);

  esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if (netif != nullptr) {
    esp_netif_ip_info_t ip{};
    if (esp_netif_get_ip_info(netif, &ip) == ESP_OK) {
      p.ip = ip.ip.addr;
      p.netmask = ip.netmask.addr;
      p.gateway = ip.gw.addr;
    }
  }
}

static ae::DataBuffer MakeFullPayload() {
  bench::FullBaselinePayload p{};
  p.type = static_cast<std::uint8_t>(bench::FullBaselineMsgType::kFull);
  ++g_sequence;
  p.sequence = g_sequence;
  p.cycle = g_cycle;
  p.uptime_ms = static_cast<std::uint32_t>(esp_timer_get_time() / 1000);
  CaptureApDiagnostics(p);
  p.construct_us = g_construct_us;
  p.select_us = g_select_us;
  p.stream_us = g_stream_us;
  p.writable_us = g_writable_us;
  // write/save/release filled after complete; this packet carries timings up to
  // writable; next cycle's packet could carry prior — for simplicity fill what
  // we know now and update write_ok after status (same packet already sent).
  p.write_us = 0;
  p.save_us = 0;
  p.release_us = 0;
  p.total_us = 0;
  p.write_ok = 0;
  return bench::EncodeFullBaseline<ae::DataBuffer>(p);
}

// Pending timings from previous completed cycle, flushed in next FULL payload.
static bench::FullBaselinePayload g_pending_timing{};
static bool g_pending_timing_valid = false;

static ae::DataBuffer MakeFullPayloadWithPending() {
  auto buf = MakeFullPayload();
  if (!g_pending_timing_valid) {
    return buf;
  }
  bench::FullBaselinePayload p{};
  std::memcpy(&p, buf.data(), sizeof(p));
  // Keep current seq/cycle/ap identity; attach previous cycle timings.
  p.construct_us = g_pending_timing.construct_us;
  p.select_us = g_pending_timing.select_us;
  p.stream_us = g_pending_timing.stream_us;
  p.writable_us = g_pending_timing.writable_us;
  p.write_us = g_pending_timing.write_us;
  p.save_us = g_pending_timing.save_us;
  p.release_us = g_pending_timing.release_us;
  p.total_us = g_pending_timing.total_us;
  p.write_ok = g_pending_timing.write_ok;
  // Encode cycle number of the completed timing into pad for clarity: use
  // pending cycle in pad1 low bits — actually store in a field we have: the
  // timings describe the previous cycle; sequence is current delivery.
  g_pending_timing_valid = false;
  return bench::EncodeFullBaseline<ae::DataBuffer>(p);
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
  if (g_had_app) {
    ForceWifiRuntimeCleanup();
  }
}

static void ConstructAether() {
  PreConstructCleanup();
  g_had_app = true;
  g_t0 = esp_timer_get_time();
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
  g_construct_us = static_cast<std::uint32_t>(SinceUs());
}

static void DoWrite() {
  if (g_write_armed) {
    return;
  }
  g_write_armed = true;
  g_writable_us = static_cast<std::uint32_t>(SinceUs());
  auto& wa = g_stream->Write(MakeFullPayloadWithPending());
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    g_write_us = static_cast<std::uint32_t>(SinceUs());
    g_write_ok = (st == ae::WriteAction::Status::kSuccess);
    auto const t_save0 = esp_timer_get_time();
    g_app->aether().Save();
    g_save_us = static_cast<std::uint32_t>(esp_timer_get_time() - t_save0);
    g_pending_full_done = true;
  });
}

static void MaybeWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  DoWrite();
}

static void OnFullClientReady(ae::Client::ptr client_ptr) {
  g_client = std::move(client_ptr);
  g_select_us = static_cast<std::uint32_t>(SinceUs());
  auto client = g_client.Load();
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             ae::P2pPortHandle{});
  g_stream_us = static_cast<std::uint32_t>(SinceUs());
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeWrite(); });
  MaybeWrite();
}

static void StartRegister() {
  g_phase = Phase::kRegister;
  g_write_armed = false;
  g_pending_register = false;
  g_exit_ok = false;
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
                       g_pending_register = true;
                     });
}

static void StartFull() {
  g_phase = Phase::kFull;
  ++g_cycle;
  g_write_armed = false;
  g_write_ok = false;
  g_pending_full_done = false;
  g_exit_ok = false;
  g_construct_us = 0;
  g_select_us = 0;
  g_stream_us = 0;
  g_writable_us = 0;
  g_write_us = 0;
  g_save_us = 0;
  g_release_us = 0;
  g_total_us = 0;
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

static void FinishRegisterInLoop() {
  g_app->aether().Save();
  g_exit_ok = true;
  g_app->Exit(0);
}

static void FinishFullInLoop() {
  g_exit_ok = g_write_ok;
  g_app->Exit(g_write_ok ? 0 : 1);
}

#endif  // ESP_PLATFORM

}  // namespace
}  // namespace temp_sensor

#if defined(ESP_PLATFORM)

void setup() {
  using namespace temp_sensor;
  nvs_flash_init();
  g_registered = false;
  g_cycle = 0;
  g_sequence = 0;
  StartRegister();
}

void loop() {
  using namespace temp_sensor;

  auto process_deferred = []() {
    if (g_app && g_pending_register) {
      g_pending_register = false;
      FinishRegisterInLoop();
      return true;
    }
    if (g_app && g_pending_full_done) {
      g_pending_full_done = false;
      FinishFullInLoop();
      return true;
    }
    return false;
  };

  if (process_deferred()) {
    return;
  }

  if (!g_app) {
    if (!g_registered) {
      StartRegister();
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

  auto const t_rel0 = esp_timer_get_time();
  ReleaseApp();
  ForceWifiRuntimeCleanup();
  g_release_us = static_cast<std::uint32_t>(esp_timer_get_time() - t_rel0);
  if (g_phase == Phase::kFull && g_t0 != 0) {
    g_total_us = static_cast<std::uint32_t>(esp_timer_get_time() - g_t0);
    g_pending_timing = {};
    g_pending_timing.construct_us = g_construct_us;
    g_pending_timing.select_us = g_select_us;
    g_pending_timing.stream_us = g_stream_us;
    g_pending_timing.writable_us = g_writable_us;
    g_pending_timing.write_us = g_write_us;
    g_pending_timing.save_us = g_save_us;
    g_pending_timing.release_us = g_release_us;
    g_pending_timing.total_us = g_total_us;
    g_pending_timing.write_ok = g_exit_ok ? 1 : 0;
    g_pending_timing_valid = true;
  }

  if (g_phase == Phase::kRegister) {
    if (g_exit_ok) {
      g_registered = true;
    }
    vTaskDelay(pdMS_TO_TICKS(kGapMs));
    if (g_registered) {
      StartFull();
    } else {
      StartRegister();
    }
    return;
  }

  // FULL cycle complete — pause outside timing, then next Construct.
  vTaskDelay(pdMS_TO_TICKS(kGapMs));
  StartFull();
}

#else

void setup() {}
void loop() {}

#endif
