/*
 * Copyright 2026 Aethernet Inc.
 *
 * Desktop Æther console receiver for prepared-message E2E bench.
 * Expects UTF-8 payloads: FULL:0 and PREPARED:1..10
 */

#include <array>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

#if defined(_WIN32)
#  include <stdlib.h>
#endif

#include "aether/all.h"

using namespace std::chrono_literals;

namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kClientName = "prepared_message_bench_rx_v1";
static constexpr int kExpectedPrepared = 10;

std::mutex g_mu;
std::vector<std::unique_ptr<ae::P2pStream>> g_streams;

bool g_full_seen = false;
std::array<int, kExpectedPrepared + 1> g_prepared_hits{};  // 1..10
int g_prepared_unique = 0;
int g_duplicates = 0;
std::vector<std::string> g_order;

std::int64_t NowMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

void PrintSummary() {
  int missing = 0;
  std::string missing_list;
  for (int i = 1; i <= kExpectedPrepared; ++i) {
    if (g_prepared_hits[static_cast<size_t>(i)] == 0) {
      ++missing;
      if (!missing_list.empty()) {
        missing_list += ",";
      }
      missing_list += std::to_string(i);
    }
  }
  std::cout << "RECEIVER\n";
  std::cout << "  full=" << (g_full_seen ? 1 : 0) << "/1\n";
  std::cout << "  prepared=" << g_prepared_unique << "/" << kExpectedPrepared
            << "\n";
  std::cout << "  missing=" << missing;
  if (missing > 0) {
    std::cout << " [" << missing_list << "]";
  }
  std::cout << "\n";
  std::cout << "  duplicates=" << g_duplicates << "\n";
  std::cout << "  order=";
  for (size_t i = 0; i < g_order.size(); ++i) {
    if (i > 0) {
      std::cout << ",";
    }
    std::cout << g_order[i];
  }
  std::cout << "\n";
  std::cout.flush();
}

void OnMessage(ae::Uid sender, ae::DataBuffer const& data) {
  auto text = std::string_view{reinterpret_cast<char const*>(data.data()),
                               data.size()};
  auto const ts = NowMs();
  auto const sender_text = ae::Format("{}", sender);

  std::lock_guard lock{g_mu};
  if (text == "FULL:0") {
    if (g_full_seen) {
      ++g_duplicates;
    }
    g_full_seen = true;
    g_order.emplace_back("FULL:0");
    std::cout << ae::Format(
        "RECV sender={} sequence=0 type=FULL receive_ts_ms={}\n", sender_text,
        ts);
  } else if (text.rfind("PREPARED:", 0) == 0) {
    auto const seq_sv = text.substr(std::string_view{"PREPARED:"}.size());
    int seq = 0;
    try {
      seq = std::stoi(std::string{seq_sv});
    } catch (...) {
      seq = -1;
    }
    if (seq >= 1 && seq <= kExpectedPrepared) {
      if (g_prepared_hits[static_cast<size_t>(seq)] > 0) {
        ++g_duplicates;
      } else {
        ++g_prepared_unique;
      }
      ++g_prepared_hits[static_cast<size_t>(seq)];
      g_order.emplace_back(std::string{text});
    }
    std::cout << ae::Format(
        "RECV sender={} sequence={} type=PREPARED receive_ts_ms={}\n",
        sender_text, seq, ts);
  } else {
    std::cout << ae::Format(
        "RECV sender={} sequence=? type=UNKNOWN receive_ts_ms={} text={}\n",
        sender_text, ts, text);
  }
  std::cout.flush();

  if (g_full_seen && g_prepared_unique == kExpectedPrepared) {
    PrintSummary();
  }
}

std::filesystem::path ResolveSessionRoot() {
#if defined(_WIN32)
  if (char const* env = std::getenv("AE_RECEIVER_SESSION_DIR")) {
    return std::filesystem::path{env};
  }
#endif
  return std::filesystem::current_path();
}

}  // namespace

int main() {
  std::cout.setf(std::ios::unitbuf);
#if defined(_WIN32)
  setvbuf(stdout, nullptr, _IONBF, 0);
#endif
  auto const session_root = ResolveSessionRoot();
  std::filesystem::create_directories(session_root / "state");
  std::filesystem::current_path(session_root);
  std::cerr << ae::Format("receiver_session_dir={}\n", session_root.string());
  std::cerr.flush();

  auto aether_app = ae::AetherApp::Construct(ae::AetherAppContext{});
  ae::Client::ptr client;
  aether_app->aether()
      ->SelectClient(kParentUid, kClientName)
      .result_event()
      .Subscribe([&](ae::Result<ae::Client::ptr, int> const& res) {
        if (!res) {
          std::cerr << "SelectClient failed\n";
          aether_app->Exit(1);
          return;
        }
        client = res.value();
        std::cout << ae::Format("RECEIVER_UID={}\n", client->uid());
        std::cout.flush();
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
              std::lock_guard lock{g_mu};
              g_streams.push_back(std::move(stream));
            });
      });

  while (!aether_app->IsExited()) {
    auto next = aether_app->Update(ae::Now());
    aether_app->WaitUntil(next);
  }
  {
    std::lock_guard lock{g_mu};
    PrintSummary();
  }
  return aether_app->ExitCode();
}
