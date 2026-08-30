/*
 * Copyright 2026 Aethernet Inc.
 *
 * FULL Aether session: N ordinary P2P Writes @ INTERVAL_MS, no deep sleep.
 * Wi-Fi / SERVICE_UID / counts via compile definitions.
 */

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>

#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "bench_payload.h"

using namespace std::chrono_literals;

#ifndef WIFI_SSID
#  error "WIFI_SSID required"
#endif
#ifndef WIFI_PASSWORD
#  error "WIFI_PASSWORD required"
#endif
#ifndef SERVICE_UID
#  error "SERVICE_UID required"
#endif

#ifndef AE_RELIABILITY_CLIENT_ID
#  define AE_RELIABILITY_CLIENT_ID "reliability_full_v1"
#endif
#ifndef AE_RELIABILITY_MSG_COUNT
#  define AE_RELIABILITY_MSG_COUNT 100
#endif
#ifndef AE_RELIABILITY_RUN_ID
#  define AE_RELIABILITY_RUN_ID 1
#endif
#ifndef AE_RELIABILITY_INTERVAL_MS
#  define AE_RELIABILITY_INTERVAL_MS 1000
#endif

namespace {

// Match AetherApp::WaitEvents: check completion after Update, before WaitUntil.
// Never WaitUntil(time_point::max()) while a finite deadline is still pending.
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

}  // namespace

void setup() {
  nvs_flash_init();

  auto const parent_uid =
      ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
  auto const service_uid = ae::Uid::FromString(SERVICE_UID);
  char const* client_id = AE_RELIABILITY_CLIENT_ID;
  std::uint32_t const run_id =
      static_cast<std::uint32_t>(AE_RELIABILITY_RUN_ID);
  int const msg_count = AE_RELIABILITY_MSG_COUNT;
  int const interval_ms = AE_RELIABILITY_INTERVAL_MS;

  ae::WiFiInit const wifi_init{
      std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
      {},
  };

  std::printf("before Construct\n");
  std::fflush(stdout);
  // Install WifiAdapter only. Clear any FS AdapterRegistry entries first —
  // registrator used to bake host EthernetAdapter into static state.
  auto app = ae::AetherApp::Construct(
      ae::AetherAppContext{}.AdaptersFactory(
          [&](ae::AetherAppContext const& ctx) {
            auto ap = ae::AdapterRegistry::ptr{ctx.aether()->adapter_registry};
            if (!ap.is_valid()) {
              ap = ae::AdapterRegistry::ptr::Create(
                  ae::CreateWith{ctx.domain()}
                      .with_id(ae::GlobalId::kAdapterRegistry)
                      .with_flags(ae::ObjFlags::kUnloadedByDefault));
            }
            auto loaded_reg = ap.Load();
            assert(loaded_reg && "AdapterRegistry load failed");
            loaded_reg->Clear();
            loaded_reg->Add(ae::WifiAdapter::ptr::Create(
                ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
                ctx.dns_resolver(), wifi_init));
            std::printf("adapter_registry_size=%zu after WifiAdapter\n",
                        loaded_reg->adapters().size());
            std::fflush(stdout);
            return ap;
          }));
  std::printf("after Construct\n");
  std::fflush(stdout);

  ae::Client::ptr client;
  bool select_done = false;
  bool select_ok = false;

  std::printf("before SelectClient id=%s\n", client_id);
  std::fflush(stdout);
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
  std::printf("after SelectClient\n");
  std::fflush(stdout);

  std::printf("before Update #1\n");
  std::fflush(stdout);
  {
    auto const t1 = app->Update(ae::Now());
    std::printf("after Update #1 done=%d ok=%d wake_is_max=%d\n",
                select_done ? 1 : 0, select_ok ? 1 : 0,
                (t1 == ae::TimePoint::max()) ? 1 : 0);
    std::fflush(stdout);
    if (!select_done) {
      app->WaitUntil(t1);
    }
  }

  std::printf("before Update #2\n");
  std::fflush(stdout);
  {
    auto const t2 = app->Update(ae::Now());
    std::printf("after Update #2 done=%d ok=%d wake_is_max=%d\n",
                select_done ? 1 : 0, select_ok ? 1 : 0,
                (t2 == ae::TimePoint::max()) ? 1 : 0);
    std::fflush(stdout);
    if (!select_done) {
      app->WaitUntil(t2);
    }
  }

  if (!select_done) {
    PumpUntil(*app, [&] { return select_done; });
  }

  std::printf("select_client_done ok=%d\n", select_ok ? 1 : 0);
  std::fflush(stdout);

  if (!select_ok) {
    for (;;) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }

  // Do not Save() into SPIFFS here: SyncDomainStorage would shadow FS_INIT with
  // a partial RW copy. Preprovisioned path must keep static state authoritative.

  std::printf("before client.Load\n");
  std::fflush(stdout);
  auto loaded = client.Load();
  std::printf("after client.Load id_str=%s cloud=%u policy=%u ccm=%u\n",
              loaded->id().c_str(),
              static_cast<unsigned>(loaded->cloud().id().id()),
              static_cast<unsigned>(loaded->connectivity_policy().id().id()),
              static_cast<unsigned>(loaded->cloud_manager().id().id()));
  std::fflush(stdout);

  // ClientConnectivityPolicy is stored with kUnloadedByDefault — must Load().
  std::printf("before connectivity_policy.Load id=%u valid=%d\n",
              static_cast<unsigned>(loaded->connectivity_policy().id().id()),
              loaded->connectivity_policy().is_valid() ? 1 : 0);
  std::fflush(stdout);
  auto policy = loaded->connectivity_policy().Load();
  if (!policy) {
    std::printf("connectivity_policy Load FAILED class=%u\n",
                static_cast<unsigned>(ae::ClientConnectivityPolicy::kClassId));
    std::fflush(stdout);
    for (;;) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }
  std::printf("after connectivity_policy.Load\n");
  std::fflush(stdout);
  policy->ResetRxTimings();
  policy->ConfigureRxTimings(ae::RequestPolicy::All{})
      .ForAllPriorities(ae::RxTimingConf::Every(1s).WithWindow(1s));
  std::printf("after ConfigureRxTimings\n");
  std::fflush(stdout);

  // CCM / Cloud are kUnloadedByDefault — must Load before P2pStream::GetCloud.
  std::printf("before cloud_manager.Load id=%u\n",
              static_cast<unsigned>(loaded->cloud_manager().id().id()));
  std::fflush(stdout);
  auto ccm = loaded->cloud_manager().Load();
  if (!ccm) {
    std::printf("cloud_manager Load FAILED\n");
    std::fflush(stdout);
    for (;;) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }
  std::printf("after cloud_manager.Load\n");
  std::fflush(stdout);

  std::printf("before cloud.Load id=%u\n",
              static_cast<unsigned>(loaded->cloud().id().id()));
  std::fflush(stdout);
  auto cloud = loaded->cloud().Load();
  if (!cloud) {
    std::printf("cloud Load FAILED\n");
    std::fflush(stdout);
    for (;;) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }
  std::printf("after cloud.Load servers=%zu\n", cloud->servers().size());
  std::fflush(stdout);

  // Host registrator persists EthernetChannel objects. On ESP those never
  // call WifiConnect, so rebuild channels from the live WifiAdapter.
  for (auto& [sid, cs] : cloud->servers()) {
    auto server = cs.server.Load();
    if (!server) {
      std::printf("server Load FAILED sid=%u\n", static_cast<unsigned>(sid));
      std::fflush(stdout);
      continue;
    }
    auto const before = server->channels.size();
    server->RebuildChannelsFromAdapters();
    std::printf("server sid=%u channels %zu -> %zu\n",
                static_cast<unsigned>(sid), before, server->channels.size());
    std::fflush(stdout);
  }

  std::printf("interval_ms=%d msg_count=%d\n", interval_ms, msg_count);
  std::fflush(stdout);

  std::printf("before P2pStream\n");
  std::fflush(stdout);
  auto stream = std::make_unique<ae::P2pStream>(
      *app, loaded, service_uid, ae::P2pPortHandle{});
  std::printf("after P2pStream\n");
  std::fflush(stdout);

  std::printf("before writable pump\n");
  std::fflush(stdout);
  PumpUntil(*app, [&] { return stream->stream_info().is_writable; });
  std::printf("stream_writable\n");
  std::fflush(stdout);

  // Modem sleep after the link is up (esp_wifi_set_ps needs Wi‑Fi started).
  (void)esp_wifi_set_ps(WIFI_PS_MAX_MODEM);
  std::printf("wifi_ps=WIFI_PS_MAX_MODEM\n");
  std::fflush(stdout);

  for (int i = 1; i <= msg_count; ++i) {
    std::printf("начал отправку seq=%d\n", i);
    std::printf("SEND_START seq=%d\n", i);
    std::fflush(stdout);

    temp_sensor::bench::ReliabilityPayload p{};
    p.type = static_cast<std::uint8_t>(
        temp_sensor::bench::ReliabilityMsgType::kFull);
    p.run_id = run_id;
    p.seq = static_cast<std::uint32_t>(i);

    ae::DataBuffer payload =
        temp_sensor::bench::EncodeReliability<ae::DataBuffer>(p);

    bool write_done = false;
    auto& wa = stream->Write(std::move(payload));
    ae::Subscription write_sub =
        wa.status_event().Subscribe([&](ae::WriteAction::Status) {
          write_done = true;
        });

    PumpUntil(*app, [&] { return write_done; });
    write_sub.Reset();

    // Pace sends by pumping the Aether task loop — do not FreeRTOS-sleep.
    auto const next_send =
        ae::Now() + std::chrono::milliseconds(interval_ms);
    PumpUntil(*app, [&] { return ae::Now() >= next_send; }, next_send);
  }

  std::printf("send_loop_done\n");
  std::fflush(stdout);

  select_sub.Reset();
  stream.reset();
  client = {};
  app.reset();

  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

void loop() {}
