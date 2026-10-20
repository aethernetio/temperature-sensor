/*
 * Minimal TCP-only Æther receiver for simple FULL + HOT 101 experiment.
 */
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <memory>
#include <string>
#include <vector>

#include "aether/all.h"

using namespace std::chrono_literals;

namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kClientName = "prepared_wifi_cache_rx_v1";

std::vector<std::unique_ptr<ae::P2pStream>> g_streams;
bool g_handlers_armed;
bool g_ready_announced;

bool TcpRxLinked(ae::Client& client) {
  for (auto* csc : client.cloud_connection().servers()) {
    auto* cc = csc->client_connection();
    if (cc->stream_info().link_state != ae::LinkState::kLinked) {
      continue;
    }
    auto ch = cc->server_connection().current_channel();
    if (auto ep = ch->endpoint()) {
      if (ep->protocol == ae::Protocol::kTcp) {
        return true;
      }
    }
  }
  return false;
}

void OnMessage(ae::Uid sender, ae::DataBuffer const& data) {
  if (data.size() != 1) {
    return;
  }
  std::cout << "MSG_NUM=" << static_cast<unsigned>(data[0])
            << " sender=" << ae::Format("{}", sender) << '\n';
}

std::filesystem::path ParseArgs(int argc, char** argv) {
  std::filesystem::path state_dir = std::filesystem::current_path();
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if ((arg == "--state-dir" || arg == "--session") && i + 1 < argc) {
      state_dir = std::filesystem::path{argv[++i]};
    } else if (arg == "--require-tcp" && i + 1 < argc) {
      ++i;
    } else if (arg == "--require-tcp") {
      continue;
    }
  }
  return state_dir;
}

}  // namespace

int main(int argc, char** argv) {
  std::cout.setf(std::ios::unitbuf);
  auto const session_root = ParseArgs(argc, argv);
  std::filesystem::create_directories(session_root / "state");
  std::filesystem::current_path(session_root);

  auto aether_app = ae::AetherApp::Construct(ae::AetherAppContext{});
  ae::Client::ptr client;
  aether_app->aether()
      ->SelectClient(kParentUid, kClientName)
      .result_event()
      .Subscribe([&](ae::Result<ae::Client::ptr, int> const& res) {
        client = res.value();
        client->connectivity_policy()->ResetRxTimings();
        client->connectivity_policy()
            ->ConfigureRxTimings(ae::RequestPolicy::All{})
            .ForAllPriorities(ae::RxTimingConf::Every(1s).WithWindow(1s));
        client->message_stream_manager().new_port_event().Subscribe(
            [&](ae::P2pPortHandle handle) {
              auto sender = handle.destination();
              auto stream = std::make_unique<ae::P2pStream>(
                  *aether_app, client.Load(), sender, std::move(handle));
              stream->out_data_event().Subscribe(
                  [sender](auto const& d) { OnMessage(sender, d); });
              g_streams.push_back(std::move(stream));
            });
        g_handlers_armed = true;
      });

  while (!aether_app->IsExited()) {
    auto t = aether_app->Update(ae::Now());
    if (!g_ready_announced && client && g_handlers_armed && TcpRxLinked(*client)) {
      std::cout << "RECEIVER_UID=" << ae::Format("{}", client->uid()) << '\n';
      g_ready_announced = true;
    }
    aether_app->WaitUntil(t);
  }
  return aether_app->ExitCode();
}
