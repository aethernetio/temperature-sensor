/*
 * Copyright 2026 Aethernet Inc.
 *
 * Phase B: existing AuthorizedApi::ping (via ae::Ping) cold FULL cycles.
 * No new server methods. Optional preferred_channel for profile hint.
 */

#include <array>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <memory>
#include <optional>
#include <variant>

#include <esp_timer.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>

#include "aether/ae_actions/ping.h"
#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "aether/wifi/wifi_probe_state.h"

using namespace std::chrono_literals;

#ifndef WIFI_SSID
#  error "WIFI_SSID required"
#endif
#ifndef WIFI_PASSWORD
#  error "WIFI_PASSWORD required"
#endif

#ifndef AE_PING_CYCLES
#  define AE_PING_CYCLES 50
#endif
#ifndef AE_PROBE_PROFILE
#  define AE_PROBE_PROFILE -1
#endif
#ifndef AE_PROBE_CHANNEL
#  define AE_PROBE_CHANNEL 0
#endif
#ifndef AE_RELIABILITY_CLIENT_ID
#  define AE_RELIABILITY_CLIENT_ID "adaptive_probe_ping_v1"
#endif

namespace {

template <typename Pred>
void PumpUntil(ae::AetherApp& app, Pred&& done,
               ae::TimePoint deadline = ae::TimePoint::max()) {
  while (!done()) {
    auto const now = ae::Now();
    if (now >= deadline) {
      return;
    }
    auto const t = app.Update(now);
    if (done() || ae::Now() >= deadline) {
      return;
    }
    auto wake = t;
    if (deadline < wake) {
      wake = deadline;
    }
    app.WaitUntil(wake);
  }
}

std::uint32_t MedianU32(std::array<std::uint32_t, 64> const& s, int n) {
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

std::uint32_t P90U32(std::array<std::uint32_t, 64> const& s, int n) {
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

struct CycleResult {
  bool ok{false};
  int ping_kind{-1};  // 0=ok 1=late 2=err 3=timeout/none
  std::uint32_t cold_full_ms{0};
  std::uint32_t wifi_ms{0};
  std::uint32_t network_ms{0};
  std::uint32_t aether_ms{0};
  std::uint32_t ping_rtt_ms{0};
  std::uint32_t release_ms{0};
};

CycleResult RunOneColdPing(int cycle_index) {
  CycleResult out{};
  auto const t_begin = esp_timer_get_time();

  nvs_flash_init();

  auto const parent_uid =
      ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
  char const* client_id = AE_RELIABILITY_CLIENT_ID;

  std::uint8_t preferred_ch = 0;
#if AE_PROBE_PROFILE >= 0
  preferred_ch = static_cast<std::uint8_t>(AE_PROBE_CHANNEL);
#endif

  ae::WiFiAp ap{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}};
  ap.preferred_channel = preferred_ch;
  ae::WiFiInit const wifi_init{std::vector<ae::WiFiAp>{ap}, {}};

  auto const t_wifi0 = esp_timer_get_time();
  auto app = ae::AetherApp::Construct(
      ae::AetherAppContext{}.AdaptersFactory(
          [&](ae::AetherAppContext const& ctx) {
            auto aptr = ae::AdapterRegistry::ptr{ctx.aether()->adapter_registry};
            if (!aptr.is_valid()) {
              aptr = ae::AdapterRegistry::ptr::Create(
                  ae::CreateWith{ctx.domain()}
                      .with_id(ae::GlobalId::kAdapterRegistry)
                      .with_flags(ae::ObjFlags::kUnloadedByDefault));
            }
            auto loaded_reg = aptr.Load();
            assert(loaded_reg && "AdapterRegistry load failed");
            loaded_reg->Clear();
            loaded_reg->Add(ae::WifiAdapter::ptr::Create(
                ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
                ctx.dns_resolver(), wifi_init));
            return aptr;
          }));
  auto const t_construct = esp_timer_get_time();

  ae::Client::ptr client;
  bool select_done = false;
  bool select_ok = false;
  ae::Subscription select_sub =
      app->aether()
          ->SelectClient(parent_uid, client_id)
          .result_event()
          .Subscribe([&](ae::Result<ae::Client::ptr, int> res) {
            select_done = true;
            if (res) {
              select_ok = true;
              client = std::move(res).value();
            }
          });

  PumpUntil(*app, [&] { return select_done; }, ae::Now() + 60s);
  auto const t_select = esp_timer_get_time();
  if (!select_ok) {
    out.cold_full_ms =
        static_cast<std::uint32_t>((esp_timer_get_time() - t_begin) / 1000);
    std::printf("B_RES cycle=%d select=0\n", cycle_index);
    std::fflush(stdout);
    return out;
  }

  auto loaded = client.Load();
  auto policy = loaded->connectivity_policy().Load();
  if (!policy) {
    std::printf("B_RES cycle=%d policy=0\n", cycle_index);
    std::fflush(stdout);
    return out;
  }
  policy->ResetRxTimings();
  policy->ConfigureRxTimings(ae::RequestPolicy::All{})
      .ForAllPriorities(ae::RxTimingConf::Every(5s).WithWindow(2s));

  auto ccm = loaded->cloud_manager().Load();
  auto cloud = loaded->cloud().Load();
  if (!ccm || !cloud) {
    std::printf("B_RES cycle=%d cloud=0\n", cycle_index);
    std::fflush(stdout);
    return out;
  }

  for (auto& [sid, cs] : cloud->servers()) {
    auto server = cs.server.Load();
    if (!server) {
      continue;
    }
    server->RebuildChannelsFromAdapters();
  }

#ifndef SERVICE_UID
#  error "SERVICE_UID required"
#endif
  auto const service_uid = ae::Uid::FromString(SERVICE_UID);
  auto stream = std::make_unique<ae::P2pStream>(
      *app, loaded, service_uid, ae::P2pPortHandle{});
  PumpUntil(*app, [&] { return stream->stream_info().is_writable; },
            ae::Now() + 90s);
  auto const t_linked = esp_timer_get_time();
  if (!stream->stream_info().is_writable) {
    out.cold_full_ms =
        static_cast<std::uint32_t>((esp_timer_get_time() - t_begin) / 1000);
    std::printf("B_RES cycle=%d linked=0\n", cycle_index);
    std::fflush(stdout);
    return out;
  }

  ae::CloudServerConnection* target = nullptr;
  ae::Channel* channel = nullptr;
  for (auto* csc : loaded->cloud_connection().servers()) {
    if (csc == nullptr) {
      continue;
    }
    auto* cc = csc->client_connection();
    if (cc == nullptr) {
      continue;
    }
    if (cc->stream_info().link_state != ae::LinkState::kLinked) {
      continue;
    }
    auto ch = cc->server_connection().current_channel();
    if (!ch) {
      continue;
    }
    target = csc;
    channel = ch.get();
    break;
  }
  if (target == nullptr || channel == nullptr) {
    std::printf("B_RES cycle=%d channel=0\n", cycle_index);
    std::fflush(stdout);
    return out;
  }

  bool ping_done = false;
  ae::Ping::PingResult ping_res = ae::Error<int>{-1};
  ae::Ping ping{ae::AeContext{*app}, *target, 5s, 2s,
                channel->ResponseTimeout()};
  ae::Subscription ping_sub =
      ping.result_event().Subscribe([&](ae::Ping::PingResult const& res) {
        ping_res = res;
        ping_done = true;
        // Record RTT into ChannelStatistics like PingCloudServers.
        std::visit(
            [channel](auto const& value) {
              using T = std::decay_t<decltype(value)>;
              if constexpr (std::is_same_v<T, ae::Ok<ae::Duration>>) {
                channel->channel_statistics().AddResponseTime(value.value);
              } else if constexpr (std::is_same_v<T, ae::Ping::LateDuration>) {
                channel->channel_statistics().AddResponseTime(value.duration);
              }
            },
            res);
      });

  auto const t_ping0 = esp_timer_get_time();
  ping.Start(ae::Now());
  PumpUntil(*app, [&] { return ping_done; }, ae::Now() + 30s);
  auto const t_ping1 = esp_timer_get_time();

  int kind = 3;
  std::uint32_t rtt_ms = 0;
  if (ping_done) {
    std::visit(
        [&](auto const& value) {
          using T = std::decay_t<decltype(value)>;
          if constexpr (std::is_same_v<T, ae::Ok<ae::Duration>>) {
            kind = 0;
            rtt_ms = static_cast<std::uint32_t>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    value.value)
                    .count());
          } else if constexpr (std::is_same_v<T, ae::Ping::LateDuration>) {
            kind = 1;
            rtt_ms = static_cast<std::uint32_t>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    value.duration)
                    .count());
          } else {
            kind = 2;
          }
        },
        ping_res);
  }

  auto const t_rel0 = esp_timer_get_time();
  ping_sub.Reset();
  select_sub.Reset();
  stream.reset();
  client = {};
  app.reset();
  auto const t_end = esp_timer_get_time();

  out.ok = (kind == 0 || kind == 1);
  out.ping_kind = kind;
  out.cold_full_ms = static_cast<std::uint32_t>((t_end - t_begin) / 1000);
  out.wifi_ms = static_cast<std::uint32_t>((t_construct - t_wifi0) / 1000);
  out.network_ms = static_cast<std::uint32_t>((t_linked - t_wifi0) / 1000);
  out.aether_ms = static_cast<std::uint32_t>((t_select - t_begin) / 1000);
  out.ping_rtt_ms = rtt_ms > 0
                        ? rtt_ms
                        : static_cast<std::uint32_t>((t_ping1 - t_ping0) / 1000);
  out.release_ms = static_cast<std::uint32_t>((t_end - t_rel0) / 1000);

  char const* kn = "timeout";
  if (kind == 0) {
    kn = "ok";
  } else if (kind == 1) {
    kn = "late";
  } else if (kind == 2) {
    kn = "error";
  }
  std::printf(
      "B_RES cycle=%d ping=%s rtt_ms=%u cold_ms=%u wifi_ms=%u net_ms=%u "
      "aether_ms=%u release_ms=%u profile_hint=%d ch=%u\n",
      cycle_index, kn, static_cast<unsigned>(out.ping_rtt_ms),
      static_cast<unsigned>(out.cold_full_ms),
      static_cast<unsigned>(out.wifi_ms),
      static_cast<unsigned>(out.network_ms),
      static_cast<unsigned>(out.aether_ms),
      static_cast<unsigned>(out.release_ms), AE_PROBE_PROFILE,
      static_cast<unsigned>(preferred_ch));
  std::fflush(stdout);
  return out;
}

}  // namespace

void setup() {
  std::printf("B_BEGIN ssid=%s cycles=%d profile_hint=%d channel=%d\n",
              WIFI_SSID, AE_PING_CYCLES, AE_PROBE_PROFILE, AE_PROBE_CHANNEL);
  std::fflush(stdout);

  int sent = 0, ok = 0, late = 0, err = 0, to = 0;
  std::array<std::uint32_t, 64> cold{};
  std::array<std::uint32_t, 64> rtt{};
  int nc = 0, nr = 0;

  for (int i = 0; i < AE_PING_CYCLES; ++i) {
    auto r = RunOneColdPing(i + 1);
    ++sent;
    if (r.ping_kind == 0) {
      ++ok;
    } else if (r.ping_kind == 1) {
      ++late;
    } else if (r.ping_kind == 2) {
      ++err;
    } else {
      ++to;
    }
    if (nc < static_cast<int>(cold.size())) {
      cold[static_cast<std::size_t>(nc++)] = r.cold_full_ms;
    }
    if ((r.ping_kind == 0 || r.ping_kind == 1) &&
        nr < static_cast<int>(rtt.size())) {
      rtt[static_cast<std::size_t>(nr++)] = r.ping_rtt_ms;
    }
    vTaskDelay(pdMS_TO_TICKS(1000));
  }

  std::printf(
      "B_SUM ping_sent=%d ping_ok=%d ping_late=%d ping_error=%d "
      "ping_timeout=%d cold_median_ms=%u cold_p90_ms=%u rtt_median_ms=%u "
      "rtt_p90_ms=%u\n",
      sent, ok, late, err, to, static_cast<unsigned>(MedianU32(cold, nc)),
      static_cast<unsigned>(P90U32(cold, nc)),
      static_cast<unsigned>(MedianU32(rtt, nr)),
      static_cast<unsigned>(P90U32(rtt, nr)));
#if AE_PROBE_PROFILE >= 0
  std::printf("B_PROFILE_NOTE preferred_channel_only=1 cached_ip_arp=NOT_TESTED\n");
#else
  std::printf("B_PROFILE_NOTE canonical=1\n");
#endif
  std::printf("B_DONE\n");
  std::fflush(stdout);
}

void loop() { vTaskDelay(pdMS_TO_TICKS(60000)); }
