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
#include <variant>

#include <esp_system.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs.h>
#include <nvs_flash.h>

#include "aether/ae_actions/ping.h"
#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"

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

constexpr int kMaxSamples = 64;
constexpr char kNvsNs[] = "ae_probe_b";

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

std::uint32_t MedianU32(std::uint32_t const* s, int n) {
  if (n <= 0) {
    return 0;
  }
  std::array<std::uint32_t, kMaxSamples> tmp{};
  for (int i = 0; i < n; ++i) {
    tmp[static_cast<std::size_t>(i)] = s[static_cast<std::size_t>(i)];
  }
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

std::uint32_t P90U32(std::uint32_t const* s, int n) {
  if (n <= 0) {
    return 0;
  }
  std::array<std::uint32_t, kMaxSamples> tmp{};
  for (int i = 0; i < n; ++i) {
    tmp[static_cast<std::size_t>(i)] = s[static_cast<std::size_t>(i)];
  }
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
  int ping_kind{-1};  // 0=ok 1=late 2=err 3=timeout/none
  std::uint32_t cold_full_ms{0};
  std::uint32_t wifi_ms{0};
  std::uint32_t network_ms{0};
  std::uint32_t aether_ms{0};
  std::uint32_t ping_rtt_ms{0};
  std::uint32_t release_ms{0};
  std::uint32_t full_write_call_us{0};
  std::uint32_t full_write_action_us{0};
};

struct CampaignState {
  int next_cycle{1};
  int sent{0};
  int ok{0};
  int late{0};
  int err{0};
  int to{0};
  int nc{0};
  int nr{0};
  int nw{0};
  std::uint32_t cold[kMaxSamples]{};
  std::uint32_t rtt[kMaxSamples]{};
  std::uint32_t write_call[kMaxSamples]{};
  std::uint32_t write_action[kMaxSamples]{};
  bool finished{false};
};

CampaignState g_campaign{};

bool LoadCampaign() {
  nvs_handle_t handle{};
  esp_err_t err = nvs_open(kNvsNs, NVS_READONLY, &handle);
  if (err != ESP_OK) {
    std::printf("B_NVS open_ro err=%d\n", static_cast<int>(err));
    std::fflush(stdout);
    return false;
  }
  std::int32_t next = 0;
  std::int32_t sent = 0;
  std::int32_t done = 0;
  std::int32_t ok = 0;
  std::int32_t late = 0;
  std::int32_t err_count = 0;
  std::int32_t to = 0;
  err = nvs_get_i32(handle, "next", &next);
  if (err != ESP_OK) {
    nvs_close(handle);
    std::printf("B_NVS get_next err=%d\n", static_cast<int>(err));
    std::fflush(stdout);
    return false;
  }
  nvs_get_i32(handle, "sent", &sent);
  nvs_get_i32(handle, "done", &done);
  nvs_get_i32(handle, "ok", &ok);
  nvs_get_i32(handle, "late", &late);
  nvs_get_i32(handle, "err", &err_count);
  nvs_get_i32(handle, "to", &to);
  std::int32_t nc = 0;
  std::int32_t nr = 0;
  std::int32_t nw = 0;
  nvs_get_i32(handle, "nc", &nc);
  nvs_get_i32(handle, "nr", &nr);
  nvs_get_i32(handle, "nw", &nw);
  size_t blob_len = sizeof(g_campaign.cold);
  if (nc > 0 && nc <= kMaxSamples) {
    if (nvs_get_blob(handle, "cold", g_campaign.cold, &blob_len) == ESP_OK) {
      g_campaign.nc = static_cast<int>(nc);
    }
  }
  blob_len = sizeof(g_campaign.rtt);
  if (nr > 0 && nr <= kMaxSamples) {
    if (nvs_get_blob(handle, "rtt", g_campaign.rtt, &blob_len) == ESP_OK) {
      g_campaign.nr = static_cast<int>(nr);
    }
  }
  blob_len = sizeof(g_campaign.write_call);
  if (nw > 0 && nw <= kMaxSamples) {
    if (nvs_get_blob(handle, "wcall", g_campaign.write_call, &blob_len) ==
        ESP_OK) {
      blob_len = sizeof(g_campaign.write_action);
      if (nvs_get_blob(handle, "wact", g_campaign.write_action, &blob_len) ==
          ESP_OK) {
        g_campaign.nw = static_cast<int>(nw);
      }
    }
  }
  nvs_close(handle);
  if (next < 1 || next > AE_PING_CYCLES + 1) {
    return false;
  }
  g_campaign.next_cycle = static_cast<int>(next);
  g_campaign.sent = static_cast<int>(sent);
  g_campaign.ok = static_cast<int>(ok);
  g_campaign.late = static_cast<int>(late);
  g_campaign.err = static_cast<int>(err_count);
  g_campaign.to = static_cast<int>(to);
  g_campaign.finished = done != 0;
  return true;
}

void SaveCampaign() {
  nvs_handle_t handle{};
  esp_err_t err = nvs_open(kNvsNs, NVS_READWRITE, &handle);
  if (err != ESP_OK) {
    std::printf("B_NVS open_rw err=%d\n", static_cast<int>(err));
    std::fflush(stdout);
    return;
  }
  err = nvs_set_i32(handle, "next", g_campaign.next_cycle);
  if (err != ESP_OK) {
    std::printf("B_NVS set_next err=%d\n", static_cast<int>(err));
  }
  nvs_set_i32(handle, "sent", g_campaign.sent);
  nvs_set_i32(handle, "done", g_campaign.finished ? 1 : 0);
  nvs_set_i32(handle, "ok", g_campaign.ok);
  nvs_set_i32(handle, "late", g_campaign.late);
  nvs_set_i32(handle, "err", g_campaign.err);
  nvs_set_i32(handle, "to", g_campaign.to);
  nvs_set_i32(handle, "nc", g_campaign.nc);
  nvs_set_i32(handle, "nr", g_campaign.nr);
  nvs_set_i32(handle, "nw", g_campaign.nw);
  if (g_campaign.nc > 0) {
    nvs_set_blob(handle, "cold", g_campaign.cold,
                 sizeof(std::uint32_t) *
                     static_cast<std::size_t>(g_campaign.nc));
  }
  if (g_campaign.nr > 0) {
    nvs_set_blob(handle, "rtt", g_campaign.rtt,
                 sizeof(std::uint32_t) *
                     static_cast<std::size_t>(g_campaign.nr));
  }
  if (g_campaign.nw > 0) {
    nvs_set_blob(handle, "wcall", g_campaign.write_call,
                 sizeof(std::uint32_t) *
                     static_cast<std::size_t>(g_campaign.nw));
    nvs_set_blob(handle, "wact", g_campaign.write_action,
                 sizeof(std::uint32_t) *
                     static_cast<std::size_t>(g_campaign.nw));
  }
  err = nvs_commit(handle);
  if (err != ESP_OK) {
    std::printf("B_NVS commit err=%d\n", static_cast<int>(err));
  }
  nvs_close(handle);
  std::printf("B_NVS saved next=%d sent=%d\n", g_campaign.next_cycle,
              g_campaign.sent);
  std::fflush(stdout);
}

void ResetCampaign() {
  g_campaign = {};
  g_campaign.next_cycle = 1;
  SaveCampaign();
}

void ReleaseApp(ae::Subscription* select_sub, ae::Subscription* ping_sub,
                std::unique_ptr<ae::P2pStream>* stream,
                ae::Client::ptr* client,
                std::unique_ptr<ae::AetherApp>* app) {
  if (ping_sub) {
    ping_sub->Reset();
  }
  if (select_sub) {
    select_sub->Reset();
  }
  if (stream) {
    stream->reset();
  }
  if (client) {
    *client = {};
  }
  if (app) {
    app->reset();
  }
}

void RecordCycle(CycleResult const& r) {
  ++g_campaign.sent;
  if (r.ping_kind == 0) {
    ++g_campaign.ok;
  } else if (r.ping_kind == 1) {
    ++g_campaign.late;
  } else if (r.ping_kind == 2) {
    ++g_campaign.err;
  } else {
    ++g_campaign.to;
  }
  if (g_campaign.nc < kMaxSamples) {
    g_campaign.cold[static_cast<std::size_t>(g_campaign.nc++)] = r.cold_full_ms;
  }
  if ((r.ping_kind == 0 || r.ping_kind == 1) && g_campaign.nr < kMaxSamples) {
    g_campaign.rtt[static_cast<std::size_t>(g_campaign.nr++)] = r.ping_rtt_ms;
  }
  if (r.full_write_call_us > 0 && g_campaign.nw < kMaxSamples) {
    g_campaign.write_call[static_cast<std::size_t>(g_campaign.nw)] =
        r.full_write_call_us;
    g_campaign.write_action[static_cast<std::size_t>(g_campaign.nw)] =
        r.full_write_action_us;
    ++g_campaign.nw;
  }
}

void PrintSummary() {
  std::printf(
      "B_SUM ping_sent=%d ping_ok=%d ping_late=%d ping_error=%d "
      "ping_timeout=%d cold_median_ms=%u cold_p90_ms=%u rtt_median_ms=%u "
      "rtt_p90_ms=%u write_call_median_us=%u write_action_median_us=%u "
      "write_call_p90_us=%u write_action_p90_us=%u\n",
      g_campaign.sent, g_campaign.ok, g_campaign.late, g_campaign.err,
      g_campaign.to, static_cast<unsigned>(MedianU32(g_campaign.cold, g_campaign.nc)),
      static_cast<unsigned>(P90U32(g_campaign.cold, g_campaign.nc)),
      static_cast<unsigned>(MedianU32(g_campaign.rtt, g_campaign.nr)),
      static_cast<unsigned>(P90U32(g_campaign.rtt, g_campaign.nr)),
      static_cast<unsigned>(MedianU32(g_campaign.write_call, g_campaign.nw)),
      static_cast<unsigned>(MedianU32(g_campaign.write_action, g_campaign.nw)),
      static_cast<unsigned>(P90U32(g_campaign.write_call, g_campaign.nw)),
      static_cast<unsigned>(P90U32(g_campaign.write_action, g_campaign.nw)));
#if AE_PROBE_PROFILE >= 0
  std::printf(
      "B_PROFILE_NOTE preferred_channel_only=1 cached_ip_arp=NOT_TESTED\n");
#else
  std::printf("B_PROFILE_NOTE canonical=1\n");
#endif
  std::printf("B_DONE\n");
  std::fflush(stdout);
}

CycleResult RunOneColdPing(int cycle_index) {
  CycleResult out{};
  auto const t_begin = esp_timer_get_time();

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
    ReleaseApp(&select_sub, nullptr, nullptr, &client, &app);
    return out;
  }

  auto loaded = client.Load();
  auto policy = loaded->connectivity_policy().Load();
  if (!policy) {
    std::printf("B_RES cycle=%d policy=0\n", cycle_index);
    std::fflush(stdout);
    ReleaseApp(&select_sub, nullptr, nullptr, &client, &app);
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
    ReleaseApp(&select_sub, nullptr, nullptr, &client, &app);
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
    ReleaseApp(&select_sub, nullptr, &stream, &client, &app);
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
    ReleaseApp(&select_sub, nullptr, &stream, &client, &app);
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

  // Small P2P Write timing: Write() call vs WriteAction complete (separate).
  if (stream->stream_info().is_writable) {
    ae::DataBuffer probe_payload;
    probe_payload.reserve(8);
    probe_payload.push_back(static_cast<std::uint8_t>('B'));
    probe_payload.push_back(static_cast<std::uint8_t>('W'));
    probe_payload.push_back(static_cast<std::uint8_t>(cycle_index & 0xff));
    probe_payload.push_back(0);
    bool write_done = false;
    auto const t_w0 = esp_timer_get_time();
    auto& wa = stream->Write(std::move(probe_payload));
    auto const t_w1 = esp_timer_get_time();
    ae::Subscription write_sub =
        wa.status_event().Subscribe([&](ae::WriteAction::Status) {
          write_done = true;
        });
    PumpUntil(*app, [&] { return write_done; }, ae::Now() + 15s);
    auto const t_w2 = esp_timer_get_time();
    out.full_write_call_us =
        static_cast<std::uint32_t>(t_w1 > t_w0 ? (t_w1 - t_w0) : 0);
    out.full_write_action_us =
        static_cast<std::uint32_t>(t_w2 > t_w0 ? (t_w2 - t_w0) : 0);
  }

  auto const t_rel0 = esp_timer_get_time();
  ReleaseApp(&select_sub, &ping_sub, &stream, &client, &app);
  auto const t_end = esp_timer_get_time();

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
      "aether_ms=%u release_ms=%u write_call_us=%u write_action_us=%u "
      "profile_hint=%d ch=%u\n",
      cycle_index, kn, static_cast<unsigned>(out.ping_rtt_ms),
      static_cast<unsigned>(out.cold_full_ms),
      static_cast<unsigned>(out.wifi_ms),
      static_cast<unsigned>(out.network_ms),
      static_cast<unsigned>(out.aether_ms),
      static_cast<unsigned>(out.release_ms),
      static_cast<unsigned>(out.full_write_call_us),
      static_cast<unsigned>(out.full_write_action_us), AE_PROBE_PROFILE,
      static_cast<unsigned>(preferred_ch));
  std::fflush(stdout);
  return out;
}

void RunNextCycle() {
  if (g_campaign.finished) {
    return;
  }
  auto const result = RunOneColdPing(g_campaign.next_cycle);
  RecordCycle(result);
  ++g_campaign.next_cycle;
  if (g_campaign.next_cycle > AE_PING_CYCLES) {
    g_campaign.finished = true;
    SaveCampaign();
    PrintSummary();
    return;
  }
  SaveCampaign();
  vTaskDelay(pdMS_TO_TICKS(200));
  esp_restart();
}

}  // namespace

void setup() {
  esp_err_t ret = nvs_flash_init();
  if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
      ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    nvs_flash_erase();
    ret = nvs_flash_init();
  }

  if (!LoadCampaign()) {
    ResetCampaign();
    std::printf("B_BEGIN ssid=%s cycles=%d profile_hint=%d channel=%d\n",
                WIFI_SSID, AE_PING_CYCLES, AE_PROBE_PROFILE, AE_PROBE_CHANNEL);
    std::fflush(stdout);
  } else {
    std::printf("B_RESUME next=%d sent=%d reason=%d\n", g_campaign.next_cycle,
                g_campaign.sent, static_cast<int>(esp_reset_reason()));
    std::fflush(stdout);
  }

  if (g_campaign.finished) {
    PrintSummary();
    return;
  }

  RunNextCycle();
}

void loop() {
  vTaskDelay(pdMS_TO_TICKS(60000));
}
