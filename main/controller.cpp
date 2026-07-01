/*
 * Copyright 2026 Aethernet Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <chrono>
#include <cstdlib>

#include "aether/all.h"
#include "sensors/sensors.h"
#include "sleeping/sleeping.h"
#include "prepared_send/prepared_send.h"

static constexpr auto kParentUid =
    ae::Uid::FromString("3ac93165-3d37-4970-87a6-fa4ee27744e4");
static constexpr auto kServiceUid =
#ifdef SERVICE_UID
    ae::Uid::FromString(SERVICE_UID);
#else
    ae::Uid::FromString("e839f1a9-e0ec-4ff6-b85c-d49efaabf24f");
#endif

#ifdef ESP_PLATFORM
static const auto kWifiCreds = ae::WifiCreds{
    /* .ssid*/ std::string{WIFI_SSID},
    /* .password*/ std::string{WIFI_PASSWORD},
};
static const auto kWifiInit = ae::WiFiInit{
    std::vector<ae::WiFiAp>{{kWifiCreds, {}}},
    ae::WiFiPowerSaveParam{},
};
#endif

void UpdateSensors();
void MessageReceived(ae::DataBuffer const& buffer);
void SendValue(std::int16_t temperature);
void GoToSleep(ae::Uap::Timer uap_timer);
void TryExportPreparedBlock();

static ae::RcPtr<ae::AetherApp> aether_app;
static ae::RcPtr<ae::P2pStream> message_stream;
static ae::Subscription stream_update_sub_;
static bool prepared_block_exported = false;

#ifndef AETHER_PREPARED_HOT_SLEEP_SECONDS
#  define AETHER_PREPARED_HOT_SLEEP_SECONDS 600
#endif

static constexpr auto kPreparedHotSleepSeconds =
    std::chrono::seconds{AETHER_PREPARED_HOT_SLEEP_SECONDS};

static constexpr std::size_t kPreparedNonceReserve =
#ifdef AETHER_PREPARED_NONCE_RESERVE
    AETHER_PREPARED_NONCE_RESERVE;
#else
    32;
#endif

void setup() {
  std::cout << ae::Format("Setup {:%Y-%m-%d %H:%M:%S}") << ae::Now()
            << std::endl;

#if defined(ESP_PLATFORM)
  {
    std::int16_t hot_temperature = {};
    ReadSensors(&hot_temperature, nullptr, nullptr, nullptr, nullptr);

    auto hot_status =
        temp_sensor::prepared_send::TryHotWakePreparedSend(hot_temperature);

    std::cout << ae::Format(" >>> Prepared hot path status: {}\n",
                            temp_sensor::prepared_send::ToString(hot_status));

    if (hot_status == temp_sensor::prepared_send::HotSendStatus::kSent) {
      auto sleep_until = std::chrono::system_clock::now() +
                         kPreparedHotSleepSeconds;
      DeepSleep(sleep_until, sleep_until, 3000);
      return;
    }
  }
#endif

  aether_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}
#if AE_DISTILLATION
#  ifdef ESP_PLATFORM
          .AddAdapterFactory([&](ae::AetherAppContext const& context) {
            return ae::WifiAdapter::ptr::Create(
                ae::CreateWith{context.domain()}.with_id(
                    ae::GlobalId::kWiFiAdapter),
                context.aether(), context.poller(), context.dns_resolver(),
                kWifiInit);
          })
#  endif
          .UapFactory([](ae::AetherAppContext const& context) {
            auto uap = context.aether()->uap;
            if (uap.is_valid()) {
              return uap;
            }
            return ae::Uap::ptr::Create(
                ae::CreateWith{context.domain()}.with_id(ae::GlobalId::kUap),
                context.aether(),
                std::initializer_list{
                    ae::Interval{.type = ae::IntervalType::kSendReceive,
                                 .duration = std::chrono::seconds{60},
                                 .window = std::chrono::seconds{10}},
                    ae::Interval{.type = ae::IntervalType::kSendOnly,
                                 .duration = std::chrono::seconds{30}},
                    ae::Interval{.type = ae::IntervalType::kSendOnly,
                                 .duration = std::chrono::seconds{30}}});
          })
#endif
  );

  aether_app->aether()->uap->sleep_event().Subscribe(GoToSleep);

  auto& select_client =
      aether_app->aether()->SelectClient(kParentUid, "Controller");

  select_client.result_event().Subscribe(
      [&](ae::Result<ae::Client::ptr, int>&& res) {
        if (res) {
          ae::Client::ptr client = std::move(res).value();
          client.WithLoaded([&](auto const& c) {
            std::cout << Format(
                "\n\n>>>>>>>\n>>>>>>> Client Loaded UID:{} \n>>>>>>> Visit "
                "https://aethernet.io/smarthub.html?uuid={} \n<<<<<\n\n",
                c->uid(), kServiceUid);

            auto handle =
                c->message_stream_manager().CreatePort(kServiceUid);
            message_stream = ae::MakeRcPtr<ae::P2pStream>(
                ae::AeContext{*aether_app}, client.Load(), kServiceUid,
                std::move(handle));
            message_stream->out_data_event().Subscribe(MessageReceived);
            stream_update_sub_ =
                message_stream->stream_update_event().Subscribe(
                    []() { TryExportPreparedBlock(); });

            UpdateSensors();
            TryExportPreparedBlock();
          });
        } else {
          std::cerr << " !!! Client selection error";
          aether_app->Exit(1);
        }
      });
}

void loop() {
  if (!aether_app) {
    return;
  }
  if (!aether_app->IsExited()) {
    auto new_time = aether_app->Update(ae::Now());
    aether_app->WaitUntil(new_time);
    TryExportPreparedBlock();
  } else {
    stream_update_sub_.Reset();
    message_stream.Reset();
    aether_app.Reset();
    prepared_block_exported = false;
  }
}

void UpdateSensors() {
  std::int16_t temperature = {};
  ReadSensors(&temperature, nullptr, nullptr, nullptr, nullptr);
  std::cout << ae::Format(" >>> Temperature: [{}]\n", temperature);
  SendValue(temperature);
}

void MessageReceived(ae::DataBuffer const& buffer) {
  std::cout << ae::Format(" >>> Received message from service: [{}]\n", buffer);
}

void TryExportPreparedBlock() {
  if (prepared_block_exported || !aether_app || !message_stream) {
    return;
  }

  if (message_stream->stream_info().link_state != ae::LinkState::kLinked) {
    return;
  }

  if (temp_sensor::prepared_send::ExportPreparedSendBlock(
          *aether_app, *message_stream, kPreparedNonceReserve)) {
    prepared_block_exported = true;
  }
}

void SendValue(std::int16_t temperature) {
  if (!message_stream) {
    return;
  }

  if (message_stream->stream_info().link_state != ae::LinkState::kLinked) {
    return;
  }

  auto message =
      temp_sensor::prepared_send::MakeTemperaturePayload(temperature);

  message_stream->Write(std::move(message)).status_event().Subscribe([](auto) {
    aether_app->aether()->uap->SleepReady();
  });
}

void GoToSleep(ae::Uap::Timer uap_timer) {
  std::cout << " >>> Going to sleep...\n";

  if (!aether_app) {
    return;
  }

  auto interval = uap_timer.interval(std::chrono::seconds{10});
  aether_app->aether().Save();
  auto sleep_until = interval.until();
  std::cout << ae::Format(
      " >>> Sleep from {:%Y-%m-%d %H:%M:%S} until {:%Y-%m-%d %H:%M:%S}...\n",
      ae::Now(), sleep_until);
  DeepSleep(interval.until(), interval.until(), 3000);
}
