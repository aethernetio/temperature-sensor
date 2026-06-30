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
#include <memory>
#include <cstdlib>

#include "aether/all.h"
#include "sensors/sensors.h"
#include "sleeping/sleeping.h"

/**
 * Standard uid for test application.
 * This is intended to use only for testing purposes due to its limitations.
 * For real applications you should register your own uid \see aethernet.io
 */
static constexpr auto kParentUid =
    ae::Uid::FromString("3ac93165-3d37-4970-87a6-fa4ee27744e4");
/**
 * \brief Uid of aether service for store the temperature values.
 * TODO: add actual uid
 */
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
    {},
};
#endif

// Update temperature sensor
void UpdateSensors();
// Message from aether service received
void MessageReceived(ae::DataBuffer const& buffer);
// Send the message value to the aether service
void SendValue(std::int16_t temperature);
// Go to sleep method
void GoToSleep(ae::Uap::Timer uap_timer);

static ae::RcPtr<ae::AetherApp> aether_app;
static std::unique_ptr<ae::P2pStream> message_stream;

void setup() {
  std::cout << ae::Format("Setup {:%Y-%m-%d %H:%M:%S}") << ae::Now()
            << std::endl;

  aether_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}
#if AE_DISTILLATION
#  ifdef ESP_PLATFORM
          // For esp32 wifi adapter configured with wifi ssid and password
          // required
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
            // configure uap
            // 60secs for send/receive
            // then 2 times by 30 seconds for send only
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

  // setup sleep on uap event
  aether_app->aether()->uap->sleep_event().Subscribe(GoToSleep);

  // select controller's client
  auto& select_client =
      aether_app->aether()->SelectClient(kParentUid, "Controller");

  select_client.result_event().Subscribe(
      [&](ae::Result<ae::Client::ptr, int>&& res) {
        if (res) {
          ae::Client::ptr client = std::move(res).value();
          client.WithLoaded([](auto const& c) {
            std::cout << Format(
                "\n\n>>>>>>>\n>>>>>>> Client Loaded UID:{} \n>>>>>>> Visit "
                "https://aethernet.io/smarthub.html?uuid={} \n<<<<<\n\n",
                c->uid(), kServiceUid);

            // open message stream to aether service client
            message_stream = std::make_unique<ae::P2pStream>(
                *aether_app, c, kServiceUid,
                c->message_stream_manager().CreatePort(kServiceUid));
            message_stream->out_data_event().Subscribe(MessageReceived);

            // measure temperature and send updated value
            UpdateSensors();
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
    // run aether update loop
    auto new_time = aether_app->Update(ae::Now());
    aether_app->WaitUntil(new_time);
  } else {
    // cleanup resources
    message_stream.reset();
    aether_app.Reset();
  }
}

// implemented in sensors/
void UpdateSensors() {
  std::int16_t temperature = {};
  std::uint32_t humidity = {};
  std::uint32_t co2 = {};
  ReadSensors(&temperature, &humidity, nullptr, &co2, nullptr);
  std::cout << ae::Format(" >>> Temperature: [{}]\n", temperature);
  std::cout << ae::Format(" >>> Humidity: [{}]\n", humidity);
  std::cout << ae::Format(" >>> Co2: [{}]\n", co2);
  // TODO: add check if wakeup cause is ulp then send value
  SendValue(temperature);
}

void MessageReceived(ae::DataBuffer const& buffer) {
  // TODO: add handle serivice's requests
  std::cout << ae::Format(" >>> Received message from service: [{}]\n", buffer);
}

void SendValue(std::int16_t temperature) {
  // The stream is not initialized yet
  if (!message_stream) {
    return;
  }

  struct Header {
    std::uint8_t const root_code = 0x3;
    std::uint8_t const size = sizeof(std::uint8_t) + sizeof(std::int16_t);
    std::uint8_t const dev_code = 0x10;
    AE_REFLECT_MEMBERS(root_code, size, dev_code)
  };
  static constexpr auto header = Header{};

  auto message = ae::DataBuffer{};
  message.reserve(sizeof(header) + 2);
  {
    auto writer = ae::VectorWriter<>{message};
    auto stream = ae::omstream{writer};
    // write message header and temperature value
    // temperature in range -100.0 to 100.0 x100 (-10000 to 10000)
    stream << header << temperature;
  }

  message_stream->Write(std::move(message)).status_event().Subscribe([](auto) {
    // with any result ready to sleep
    aether_app->aether()->uap->SleepReady();
  });
}

void GoToSleep(ae::Uap::Timer uap_timer) {
  std::cout << " >>> Going to sleep...\n";

  if (!aether_app) {
    return;
  }

  // get the interval with the specified offset
  // offset is required to account the Save operation
  auto interval = uap_timer.interval(std::chrono::seconds{10});
  // save current aether state
  aether_app->aether().Save();
  // Go to sleep
  auto sleep_until = interval.until();
  std::cout << ae::Format(
      " >>> Sleep from {:%Y-%m-%d %H:%M:%S} until {:%Y-%m-%d %H:%M:%S}...\n",
      ae::Now(), sleep_until);
  // TODO: add separate sleep duration
  DeepSleep(interval.until(), interval.until(),
            3000);  // wait till time or 30 deegrees
}
