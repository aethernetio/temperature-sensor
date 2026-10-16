/*
 * Thermometer 2 / ESP32-C6: one ordinary P2pStream FULL_READY to desktop receiver.
 * Desktop sender pattern (CreatePort + Write); ESP Wi-Fi init from FULL baseline.
 */

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>

#include <esp_event.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>

#include "aether/all.h"
#include "aether/ae_exp_wifi.h"
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

#ifndef AE_ORDINARY_FULL_READY_CLIENT_ID
#  define AE_ORDINARY_FULL_READY_CLIENT_ID "reliability_full_v1"
#endif
#ifndef AE_ORDINARY_FULL_READY_RUN_ID
#  define AE_ORDINARY_FULL_READY_RUN_ID 920001u
#endif

namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");

char const* ResetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:
      return "POWERON";
    case ESP_RST_SW:
      return "SW";
    case ESP_RST_PANIC:
      return "PANIC";
    case ESP_RST_INT_WDT:
      return "INT_WDT";
    case ESP_RST_TASK_WDT:
      return "TASK_WDT";
    case ESP_RST_WDT:
      return "WDT";
    case ESP_RST_DEEPSLEEP:
      return "DEEPSLEEP";
    case ESP_RST_BROWNOUT:
      return "BROWNOUT";
    default:
      return "OTHER";
  }
}

char const* LinkStateName(ae::LinkState s) {
  if (s == ae::LinkState::kUnlinked) {
    return "unlinked";
  }
  if (s == ae::LinkState::kLinked) {
    return "linked";
  }
  if (s == ae::LinkState::kLinkError) {
    return "link_error";
  }
  return "unknown";
}

char const* WriteStatusName(ae::WriteAction::Status st) {
  switch (st) {
    case ae::WriteAction::Status::kSuccess:
      return "success";
    case ae::WriteAction::Status::kFail:
      return "fail";
    case ae::WriteAction::Status::kStop:
      return "stop";
    default:
      return "unknown";
  }
}

void LogStreamState(ae::P2pStream const& stream) {
  auto const info = stream.stream_info();
  std::printf("STREAM_STATE link=%s writable=%d reliable=%d\n",
              LinkStateName(info.link_state), info.is_writable ? 1 : 0,
              info.is_reliable ? 1 : 0);
  std::fflush(stdout);
}

void RebuildCloudServerChannelsFromLiveRegistry(ae::AetherApp& app,
                                                ae::Cloud& cloud) {
  auto live_reg = app.aether()->adapter_registry.Load();
  if (!live_reg) {
    return;
  }
  for (auto& [sid, cs] : cloud.servers()) {
    auto server = cs.server.Load();
    if (!server) {
      continue;
    }
    server->channels.clear();
    ae::Adapter::ptr wifi_adapter;
    for (auto it = live_reg->adapters().rbegin();
         it != live_reg->adapters().rend(); ++it) {
      if (ae::WifiAdapter::ptr{*it}.is_valid()) {
        wifi_adapter = *it;
        break;
      }
    }
    if (!wifi_adapter) {
      continue;
    }
    auto loaded_wifi = wifi_adapter.Load();
    for (auto const& ap : loaded_wifi->access_points()) {
      auto new_channels = ap->GenerateChannels(cs.server);
      server->channels.insert(std::end(server->channels),
                              std::begin(new_channels),
                              std::end(new_channels));
    }
  }
}

void LogCloudServers(ae::Client::ptr const& client) {
  auto loaded = client.Load();
  auto cloud = loaded->cloud().Load();
  if (!cloud) {
    std::printf("CLOUD_LOAD fail\n");
    std::fflush(stdout);
    return;
  }
  std::printf("CLOUD_SERVERS count=%zu\n", cloud->servers().size());
  for (auto& [sid, cs] : cloud->servers()) {
    auto server = cs.server.Load();
    if (!server) {
      std::printf("SERVER sid=%u load_fail\n", static_cast<unsigned>(sid));
      continue;
    }
    std::printf("SERVER sid=%u channels=%zu\n", static_cast<unsigned>(sid),
                server->channels.size());
    for (auto const& ch : server->channels) {
      if (ch && ch->endpoint()) {
        std::printf("CHANNEL sid=%u endpoint=%s protocol=%u\n",
                    static_cast<unsigned>(sid),
                    ae::Format("{}", *ch->endpoint()).c_str(),
                    static_cast<unsigned>(ch->endpoint()->protocol));
      }
    }
  }
  std::fflush(stdout);
}

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
  // IDF may install a default loop before setup(); EspWifiDriver creates its own.
  esp_event_loop_delete_default();
  auto const reset = esp_reset_reason();
  std::printf("BOOT reset_reason=%s (%d)\n", ResetReasonName(reset),
              static_cast<int>(reset));
  std::printf("AWAKE_USB_WINDOW begin 60s\n");
  std::fflush(stdout);
  vTaskDelay(pdMS_TO_TICKS(60000));

  auto const service_uid = ae::Uid::FromString(SERVICE_UID);
  char const* client_id = AE_ORDINARY_FULL_READY_CLIENT_ID;
  std::uint32_t const run_id = AE_ORDINARY_FULL_READY_RUN_ID;

  std::printf("FS_INIT client_id=%s parent=%s\n", client_id,
              "b1ac52c8-8d94-bd39-4c01-a631ac594165");
  std::printf("DESTINATION_UID=%s\n", SERVICE_UID);
  std::printf("FULL_READY run_id=%lu\n", static_cast<unsigned long>(run_id));
  std::fflush(stdout);

  ae::WiFiInit const wifi_init{
      std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
      {},
  };

  esp_event_loop_delete_default();
  std::printf("APP_CONSTRUCT_BEGIN\n");
  std::fflush(stdout);
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
            bool have_wifi = false;
            for (auto const& adapter : loaded_reg->adapters()) {
              if (ae::WifiAdapter::ptr{adapter}.is_valid()) {
                have_wifi = true;
                break;
              }
            }
            if (!have_wifi) {
              loaded_reg->Add(ae::WifiAdapter::ptr::Create(
                  ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
                  ctx.dns_resolver(), wifi_init));
            }
            std::printf("ADAPTERS count=%zu\n", loaded_reg->adapters().size());
            std::fflush(stdout);
            return ap;
          }));
  std::printf("APP_CONSTRUCT_DONE\n");
  std::fflush(stdout);

  ae::Client::ptr client;
  bool select_done = false;
  bool select_ok = false;

  std::printf("SELECT_CLIENT_BEGIN id=%s\n", client_id);
  std::fflush(stdout);
  ae::Subscription select_sub =
      app->aether()
          ->SelectClient(kParentUid, client_id)
          .result_event()
          .Subscribe([&](ae::Result<ae::Client::ptr, int> res) {
            select_done = true;
            if (res) {
              select_ok = true;
              client = std::move(res).value();
            }
          });

  PumpUntil(*app, [&] { return select_done; }, ae::Now() + 120s);
  std::printf("SELECT_CLIENT_%s\n", select_ok ? "SUCCESS" : "FAIL");
  if (select_ok) {
    std::printf("SENDER_UID=%s\n", ae::Format("{}", client->uid()).c_str());
  }
  std::fflush(stdout);

  if (!select_ok) {
    for (;;) {
      vTaskDelay(pdMS_TO_TICKS(1000));
    }
  }

  auto loaded = client.Load();
  auto policy = loaded->connectivity_policy().Load();
  policy->ResetRxTimings();
  policy->ConfigureRxTimings(ae::RequestPolicy::All{})
      .ForAllPriorities(ae::RxTimingConf::Every(1s).WithWindow(1s));

  (void)loaded->cloud_manager().Load();
  auto cloud = loaded->cloud().Load();
  if (cloud) {
    RebuildCloudServerChannelsFromLiveRegistry(*app, *cloud);
  }
  LogCloudServers(client);

  std::printf("STREAM_CREATED destination=%s\n", SERVICE_UID);
  std::fflush(stdout);
  auto stream = std::make_unique<ae::P2pStream>(
      *app, loaded, service_uid,
      loaded->message_stream_manager().CreatePort(service_uid));
  LogStreamState(*stream);

  ae::Subscription stream_update_sub =
      stream->stream_update_event().Subscribe([&]() {
        std::printf("STREAM_UPDATE\n");
        LogStreamState(*stream);
      });

  // Desktop defers first Write 1s on Ethernet; ESP Wi-Fi needs longer association.
  auto const write_not_before = ae::Now() + 20s;
  while (ae::Now() < write_not_before) {
    auto t = app->Update(ae::Now());
    app->WaitUntil(t < write_not_before ? t : write_not_before);
  }

  std::printf("FULL_READY_WRITE_BEGIN run_id=%lu\n",
              static_cast<unsigned long>(run_id));
  LogStreamState(*stream);
  std::fflush(stdout);

  temp_sensor::bench::ReliabilityPayload p{};
  p.type = static_cast<std::uint8_t>(
      temp_sensor::bench::ReliabilityMsgType::kFullReady);
  p.run_id = run_id;
  p.seq = 0;
  p.prev_status = 0;
  ae::DataBuffer payload =
      temp_sensor::bench::EncodeReliability<ae::DataBuffer>(p);

  bool write_terminal = false;
  char write_status[16] = "pending";
  auto& wa = stream->Write(std::move(payload));
  ae::Subscription write_sub = wa.status_event().Subscribe(
      [&](ae::WriteAction::Status st) {
        if (st == ae::WriteAction::Status::kSuccess ||
            st == ae::WriteAction::Status::kFail ||
            st == ae::WriteAction::Status::kStop) {
          write_terminal = true;
          std::strncpy(write_status, WriteStatusName(st), sizeof(write_status) - 1);
          std::printf("FULL_READY_WRITE_TERMINAL status=%s run_id=%lu\n",
                      write_status, static_cast<unsigned long>(run_id));
          std::fflush(stdout);
        }
      });

  auto const post_write_deadline = ae::Now() + 180s;
  while (ae::Now() < post_write_deadline) {
    PumpUntil(*app, [&] { return write_terminal; }, ae::Now() + 1s);
    if (write_terminal) {
      break;
    }
  }

  LogCloudServers(client);
  std::printf("POST_WRITE_PUMP remaining\n");
  std::fflush(stdout);
  while (ae::Now() < post_write_deadline) {
    auto t = app->Update(ae::Now());
    app->WaitUntil(t);
  }

  std::printf("FUNCTIONAL_CONTROL_DONE write_status=%s\n", write_status);
  std::fflush(stdout);

  for (;;) {
    auto t = app->Update(ae::Now());
    app->WaitUntil(t);
  }
}

void loop() {}
