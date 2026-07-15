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
#include <iostream>

#include "aether/all.h"
#include "sensors/sensors.h"
#include "sleeping/sleeping.h"

using namespace std::chrono_literals;

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

struct WorkMode {
  using type = std::uint8_t;
  static constexpr type kTx = 0x1, kRx = 0x2, kTxRx = 0x3;

  static std::string_view ToText(type v) {
    switch (v) {
      case WorkMode::kTx:
        return "TX";
      case WorkMode::kRx:
        return "RX";
      case WorkMode::kTxRx:
        return "TX+RX";
      default:
        return "NONE";
    }
  }
};

static constexpr ae::Duration kTxInterval = 30s;
static RTC_STORAGE_ATTR ae::TimePoint next_tx_time = {};

// Client selection handler
void ClientSelected(ae::Result<ae::Client::ptr, int> res);
// Update temperature sensor
void UpdateSensors();
// Message from aether service received
void MessageReceived(ae::DataBuffer const& buffer);
// Send the message value to the aether service
void SendValue(std::int16_t temperature);
// Make all required work and ready to sleep
void SleepReady();
// Go to sleep method
void GoToSleep(ae::TimePoint time_point);

static ae::RcPtr<ae::AetherApp> aether_app;
static ae::Client::ptr client;
static std::unique_ptr<ae::P2pStream> message_stream;

void setup() {
  std::cout << ae::Format("Setup {:%Y-%m-%d %H:%M:%S}\n") << ae::Now();

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
#endif
  );

  // select controller's client
  aether_app->aether()
      ->SelectClient(kParentUid, "Controller")
      .result_event()
      .Subscribe(&ClientSelected);
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

void ClientSelected(ae::Result<ae::Client::ptr, int> res) {
  if (!res) {
    std::cerr << " !!! Client selection error\n";
    aether_app->Exit(1);
    return;
  }

  client = std::move(res).value();
  auto r = client.WithLoaded([](ae::Ptr<ae::Client> const& c) {
    std::cout << ae::Format(
        "\n\n>>>>>>>\n>>>>>>> Client Loaded UID:{} \n>>>>>>> Visit "
        "https://aethernet.io/smarthub.html?uuid={} \n<<<<<\n\n",
        c->uid(), kServiceUid);

    // Config connectivity policy, open 5s RX window every 60s.
    c->connectivity_policy()
        ->ConfigureRxTimings(ae::RequestPolicy::All{})
        .ForAllPriorities(ae::RxTimingConf::Every(60s).WithWindow(5s));

    // check current work_mode
    // it's always TX if we woke up
    auto work_mode = WorkMode::kTx;
    auto current_time = ae::Now();
    static constexpr ae::Duration threshold = 5s;
    auto cp_status = c->connectivity_policy()->GetStatus();
    // if it's next_service_time it's also RX
    if ((current_time + threshold) >= cp_status.next_service_time) {
      work_mode = work_mode | WorkMode::kRx;
    }
    std::cout << ae::Format(">>>> Run in {} work mode\n",
                            WorkMode::ToText(work_mode));

    if ((work_mode & WorkMode::kRx) != 0) {
      // open message stream for receive and send
      message_stream = std::make_unique<ae::P2pStream>(
          *aether_app, c, kServiceUid,
          c->message_stream_manager().CreatePort(kServiceUid));
      message_stream->out_data_event().Subscribe(MessageReceived);
    } else {
      // open message stream for send only
      message_stream = std::make_unique<ae::P2pStream>(
          *aether_app, c, kServiceUid, ae::P2pPortHandle{});
    }

    // measure temperature and send updated value
    UpdateSensors();
  });

  if (!r) {
    std::cerr << " !!! Client wasn't loaded";
    aether_app->Exit(2);
  }
}

// implemented in sensors/
void UpdateSensors() {
  std::int16_t temperature = {};
  std::uint32_t humidity = {};
  std::uint32_t co2 = {};
  ReadSensors(&temperature, &humidity, nullptr, &co2, nullptr);
  std::cout << ae::Format(" >>> Temperature: [{}], Humidity: [{}], CO2: [{}]\n",
                          temperature, humidity, co2);
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
    SleepReady();
  });

  next_tx_time = ae::Now() + kTxInterval;
}

void SleepReady() {
  auto go_to_sleep = [](auto next_rx_time) noexcept {
    auto next_time = std::min(next_tx_time, next_rx_time);
    std::cout << ae::Format(
        ">> Go to sleep no wait, next_tx_time {}, next_rx_time {}\n",
        next_tx_time, next_rx_time);
    GoToSleep(next_time);
  };

  auto status = client->connectivity_policy()->GetStatus();
  if (status.can_suspend) {
    go_to_sleep(status.next_service_time);
  } else {
    std::cout << ">>> Wait for can suspend\n";
    client->connectivity_policy()->suspend_allowed_event().Subscribe([&]() {
      go_to_sleep(client->connectivity_policy()->GetStatus().next_service_time);
    });
  }
}

void GoToSleep(ae::TimePoint time_point) {
  std::cout << " >>> Going to sleep...\n";

  if (!aether_app) {
    return;
  }
  // save current aether state
  aether_app->aether().Save();

  // Go to sleep
  std::cout << ae::Format(
      " >>> Sleep from {:%Y-%m-%d %H:%M:%S} until {:%Y-%m-%d %H:%M:%S}...\n",
      ae::Now(), time_point);
  // TODO: add separate sleep duration
  DeepSleep(time_point, time_point, 3000);  // wait till time or 30 deegrees
}
