/*
 * Minimal TCP Æther receiver for simple FULL + HOT 101 experiment.
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
bool g_require_tcp = false;

void EnforceTcp(ae::Client& client) {
  if (!g_require_tcp) {
    return;
  }
  for (auto* csc : client.cloud_connection().servers()) {
    if (csc == nullptr) {
      continue;
    }
    auto* cc = csc->client_connection();
    if (cc == nullptr ||
        cc->stream_info().link_state != ae::LinkState::kLinked) {
      continue;
    }
    auto ch = cc->server_connection().current_channel();
    if (!ch) {
      continue;
    }
    if (auto ep = ch->endpoint()) {
      if (ep->protocol != ae::Protocol::kTcp) {
        csc->Restream();
      }
    }
  }
}

void OnMessage(ae::Uid sender, ae::DataBuffer const& data) {
  if (data.size() == 1) {
    std::cout << "MSG_NUM=" << static_cast<unsigned>(data[0])
              << " sender=" << ae::Format("{}", sender) << '\n';
    return;
  }
  if (data.size() >= 1 && data.size() <= 8) {
    bool digits = true;
    for (auto b : data) {
      if (b < '0' || b > '9') {
        digits = false;
        break;
      }
    }
    if (digits) {
      std::cout << "MSG_NUM=" << std::string(data.begin(), data.end())
                << " sender=" << ae::Format("{}", sender) << '\n';
    }
  }
}

std::filesystem::path ParseStateDir(int argc, char** argv) {
  std::filesystem::path state_dir = std::filesystem::current_path();
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if ((arg == "--state-dir" || arg == "--session") && i + 1 < argc) {
      state_dir = std::filesystem::path{argv[++i]};
    } else if (arg == "--require-tcp" || arg == "--require-tcp=1") {
      g_require_tcp = true;
    } else if (arg.rfind("--require-tcp=", 0) == 0) {
      g_require_tcp = (arg.substr(14) != "0");
    }
  }
  return state_dir;
}

}  // namespace

int main(int argc, char** argv) {
  std::cout.setf(std::ios::unitbuf);
  auto const session_root = ParseStateDir(argc, argv);
  std::filesystem::create_directories(session_root / "state");
  std::filesystem::current_path(session_root);

  auto aether_app = ae::AetherApp::Construct(ae::AetherAppContext{});
  ae::Client::ptr client;
  aether_app->aether()
      ->SelectClient(kParentUid, kClientName)
      .result_event()
      .Subscribe([&](ae::Result<ae::Client::ptr, int> const& res) {
        client = res.value();
        std::cout << "RECEIVER_UID=" << ae::Format("{}", client->uid()) << '\n';
        client->connectivity_policy()->ResetRxTimings();
        client->connectivity_policy()
            ->ConfigureRxTimings(ae::RequestPolicy::All{})
            .ForAllPriorities(ae::RxTimingConf::Every(1s).WithWindow(1s));
        EnforceTcp(*client);
        client->cloud_connection().servers_update_event().Subscribe(
            [client]() { EnforceTcp(*client); });
        client->message_stream_manager().new_port_event().Subscribe(
            [&](ae::P2pPortHandle handle) {
              auto sender = handle.destination();
              auto stream = std::make_unique<ae::P2pStream>(
                  *aether_app, client.Load(), sender, std::move(handle));
              stream->out_data_event().Subscribe(
                  [sender](auto const& d) { OnMessage(sender, d); });
              g_streams.push_back(std::move(stream));
            });
      });

  while (!aether_app->IsExited()) {
    auto t = aether_app->Update(ae::Now());
    if (client) {
      EnforceTcp(*client);
    }
    aether_app->WaitUntil(t);
  }
  return aether_app->ExitCode();
}
