/*
 * Copyright 2026 Aethernet Inc.
 *
 * Desktop Æther receiver for silent prepared Wi-Fi cache 5x20 experiment.
 * Decodes binary bench payloads and prints aggregate statistics.
 */

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#if defined(_WIN32)
#  include <stdlib.h>
#endif

#include "aether/all.h"
#include "bench_payload.h"

using namespace std::chrono_literals;

namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kClientName = "prepared_wifi_cache_rx_v1";
static constexpr int kOuter = 5;
static constexpr int kPreparedPer = 20;
static constexpr int kExpectedFull = kOuter;
static constexpr int kExpectedPrepared = kOuter * kPreparedPer;
static constexpr int kExpectedApp = kExpectedFull + kExpectedPrepared;  // 105
static constexpr int kExpectedTotal = kExpectedApp + 1;                 // +FINAL

std::mutex g_mu;
std::vector<std::unique_ptr<ae::P2pStream>> g_streams;

std::uint32_t g_registration_us = 0;
std::array<std::uint32_t, kOuter> g_full_us{};
std::array<bool, kOuter> g_full_have{};
std::array<std::array<std::uint32_t, kPreparedPer>, kOuter> g_prep_us{};
std::array<std::array<bool, kPreparedPer>, kOuter> g_prep_have{};
std::array<std::array<std::uint8_t, kPreparedPer>, kOuter> g_prep_flags{};

int g_full_recv = 0;
int g_prep_recv = 0;
int g_final_recv = 0;
int g_duplicates = 0;
int g_out_of_order = 0;
int g_last_seq = 0;
bool g_done = false;

std::vector<std::uint16_t> g_seen_seq;

std::int64_t NowMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::uint32_t Percentile(std::vector<std::uint32_t> v, int pct) {
  if (v.empty()) {
    return 0;
  }
  std::sort(v.begin(), v.end());
  auto const idx = (pct * (static_cast<int>(v.size()) - 1) + 99) / 100;
  return v[static_cast<size_t>(idx)];
}

void PrintSummary() {
  std::vector<std::uint32_t> fulls;
  std::vector<std::uint32_t> firsts;
  std::vector<std::uint32_t> warms;
  std::vector<std::uint32_t> all;
  int bssid_hits = 0;
  int ip_hits = 0;
  int dhcp_skip = 0;
  int static_arp_hits = 0;
  int arp_fallback_hits = 0;
  int wifi_fallback_hits = 0;

  for (int o = 0; o < kOuter; ++o) {
    if (g_full_have[static_cast<size_t>(o)]) {
      fulls.push_back(g_full_us[static_cast<size_t>(o)]);
    }
    for (int i = 0; i < kPreparedPer; ++i) {
      if (!g_prep_have[static_cast<size_t>(o)][static_cast<size_t>(i)]) {
        continue;
      }
      auto const us = g_prep_us[static_cast<size_t>(o)][static_cast<size_t>(i)];
      auto const fl = g_prep_flags[static_cast<size_t>(o)][static_cast<size_t>(i)];
      all.push_back(us);
      if (i == 0) {
        firsts.push_back(us);
      } else {
        warms.push_back(us);
      }
      if (fl & static_cast<std::uint8_t>(temp_sensor::bench::CacheFlags::kUsedBssid)) {
        ++bssid_hits;
      }
      if (fl & static_cast<std::uint8_t>(temp_sensor::bench::CacheFlags::kUsedStaticIp)) {
        ++ip_hits;
      }
      if (fl & static_cast<std::uint8_t>(temp_sensor::bench::CacheFlags::kDhcpSkipped)) {
        ++dhcp_skip;
      }
      if (fl & static_cast<std::uint8_t>(temp_sensor::bench::CacheFlags::kUsedStaticArp)) {
        ++static_arp_hits;
      }
      if (fl & static_cast<std::uint8_t>(temp_sensor::bench::CacheFlags::kArpFallback)) {
        ++arp_fallback_hits;
      }
      if (fl & static_cast<std::uint8_t>(temp_sensor::bench::CacheFlags::kWifiFallback)) {
        ++wifi_fallback_hits;
      }
    }
  }

  auto print_vec = [](char const* name, std::vector<std::uint32_t> const& v) {
    std::cout << name << " raw=[";
    for (size_t i = 0; i < v.size(); ++i) {
      if (i) {
        std::cout << ", ";
      }
      std::cout << v[i];
    }
    std::cout << "]\n";
    if (v.empty()) {
      return;
    }
    auto sorted = v;
    std::sort(sorted.begin(), sorted.end());
    std::cout << "  min=" << sorted.front() << "\n";
    std::cout << "  median=" << Percentile(v, 50) << "\n";
    if (v.size() >= 10) {
      std::cout << "  p90=" << Percentile(v, 90) << "\n";
      std::cout << "  p99=" << Percentile(v, 99) << "\n";
    }
    std::cout << "  max=" << sorted.back() << "\n";
    std::cout << "  n=" << v.size() << "\n";
  };

  int missing = 0;
  for (int s = 1; s <= kExpectedApp; ++s) {
    if (std::find(g_seen_seq.begin(), g_seen_seq.end(),
                  static_cast<std::uint16_t>(s)) == g_seen_seq.end()) {
      ++missing;
    }
  }

  std::cout << "REGISTRATION\n";
  std::cout << "  time_us=" << g_registration_us << "\n";
  std::cout << "FULL\n";
  print_vec("  ", fulls);
  std::cout << "FIRST PREPARED\n";
  print_vec("  ", firsts);
  std::cout << "WARM PREPARED\n";
  print_vec("  ", warms);
  std::cout << "ALL PREPARED\n";
  print_vec("  ", all);
  std::cout << "DELIVERY\n";
  std::cout << "  full=" << g_full_recv << "/" << kExpectedFull << "\n";
  std::cout << "  prepared=" << g_prep_recv << "/" << kExpectedPrepared << "\n";
  std::cout << "  final=" << g_final_recv << "/1\n";
  std::cout << "  missing=" << missing << "\n";
  std::cout << "  duplicates=" << g_duplicates << "\n";
  std::cout << "  out_of_order=" << g_out_of_order << "\n";
  std::cout << "CACHE\n";
  std::cout << "  BSSID reuse confirmed="
            << (bssid_hits > 0 ? "yes" : "no") << " (hits=" << bssid_hits
            << ")\n";
  std::cout << "  channel reuse yes/no via BSSID flag hits=" << bssid_hits
            << "\n";
  std::cout << "  static IP reuse confirmed="
            << (ip_hits > 0 ? "yes" : "no") << " (hits=" << ip_hits << ")\n";
  std::cout << "  DHCP skipped confirmed="
            << (dhcp_skip > 0 ? "yes" : "no") << " (hits=" << dhcp_skip
            << ")\n";
  std::cout << "  used_static_arp hits=" << static_arp_hits << "\n";
  std::cout << "  arp_fallback hits=" << arp_fallback_hits << "\n";
  std::cout << "  wifi_fallback hits=" << wifi_fallback_hits << "\n";
  std::cout << "NOTE prepared timing includes AETHER_PREPARED_POST_SEND_HOLD_MS=300\n";
  std::cout << "BENCH_DONE\n";
  std::cout.flush();
}

void OnMessage(ae::Uid sender, ae::DataBuffer const& data) {
  temp_sensor::bench::Payload p{};
  if (!temp_sensor::bench::Decode(data, p)) {
    std::cout << "RECV unknown sender=" << ae::Format("{}", sender)
              << " size=" << data.size() << "\n";
    return;
  }

  auto const ts = NowMs();
  std::lock_guard lock{g_mu};

  if (std::find(g_seen_seq.begin(), g_seen_seq.end(), p.sequence_global) !=
      g_seen_seq.end()) {
    ++g_duplicates;
  } else {
    g_seen_seq.push_back(p.sequence_global);
  }
  if (p.sequence_global != 0 && g_last_seq != 0 &&
      p.sequence_global < static_cast<std::uint16_t>(g_last_seq)) {
    ++g_out_of_order;
  }
  g_last_seq = p.sequence_global;

  auto type = static_cast<temp_sensor::bench::MsgType>(p.type);
  if (type == temp_sensor::bench::MsgType::kFull) {
    ++g_full_recv;
    if (p.outer_cycle == 1 && p.registration_us != 0) {
      g_registration_us = p.registration_us;
    }
    // previous_full_us is timing of outer_cycle-1
    if (p.outer_cycle >= 2 && p.outer_cycle <= kOuter + 1) {
      int const idx = static_cast<int>(p.outer_cycle) - 2;
      if (idx >= 0 && idx < kOuter && p.previous_full_us != 0) {
        g_full_us[static_cast<size_t>(idx)] = p.previous_full_us;
        g_full_have[static_cast<size_t>(idx)] = true;
      }
    }
    // previous_prepared_us is last prepared of previous outer
    if (p.outer_cycle >= 2 && p.previous_prepared_us != 0) {
      int const o = static_cast<int>(p.outer_cycle) - 2;
      if (o >= 0 && o < kOuter) {
        g_prep_us[static_cast<size_t>(o)][kPreparedPer - 1] =
            p.previous_prepared_us;
        g_prep_have[static_cast<size_t>(o)][kPreparedPer - 1] = true;
        g_prep_flags[static_cast<size_t>(o)][kPreparedPer - 1] = p.cache_flags;
      }
    }
    std::cout << ae::Format(
        "RECV FULL outer={} seq={} reg_us={} prev_full_us={} prev_prep_us={} "
        "ts={}\n",
        p.outer_cycle, p.sequence_global, p.registration_us, p.previous_full_us,
        p.previous_prepared_us, ts);
  } else if (type == temp_sensor::bench::MsgType::kPrepared) {
    ++g_prep_recv;
    int const o = static_cast<int>(p.outer_cycle) - 1;
    int const i = static_cast<int>(p.prepared_index) - 1;
    // previous_prepared_us is timing of prepared_index-1
    if (o >= 0 && o < kOuter && p.prepared_index >= 2) {
      int const pi = static_cast<int>(p.prepared_index) - 2;
      if (pi >= 0 && pi < kPreparedPer) {
        g_prep_us[static_cast<size_t>(o)][static_cast<size_t>(pi)] =
            p.previous_prepared_us;
        g_prep_have[static_cast<size_t>(o)][static_cast<size_t>(pi)] = true;
        g_prep_flags[static_cast<size_t>(o)][static_cast<size_t>(pi)] =
            p.cache_flags;
      }
    }
    std::cout << ae::Format(
        "RECV PREPARED outer={} idx={} seq={} prev_us={} flags={} ts={}\n",
        p.outer_cycle, p.prepared_index, p.sequence_global,
        p.previous_prepared_us, p.cache_flags, ts);
  } else if (type == temp_sensor::bench::MsgType::kFinal) {
    ++g_final_recv;
    if (p.previous_full_us != 0) {
      g_full_us[kOuter - 1] = p.previous_full_us;
      g_full_have[kOuter - 1] = true;
    }
    if (p.previous_prepared_us != 0) {
      g_prep_us[kOuter - 1][kPreparedPer - 1] = p.previous_prepared_us;
      g_prep_have[kOuter - 1][kPreparedPer - 1] = true;
      g_prep_flags[kOuter - 1][kPreparedPer - 1] = p.cache_flags;
    }
    if (p.registration_us != 0) {
      g_registration_us = p.registration_us;
    }
    std::cout << ae::Format(
        "RECV FINAL prev_full_us={} prev_prep_us={} flags={} ts={}\n",
        p.previous_full_us, p.previous_prepared_us, p.cache_flags, ts);
    PrintSummary();
    g_done = true;
  }
  std::cout.flush();
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
    {
      std::lock_guard lock{g_mu};
      if (g_done) {
        aether_app->Exit(0);
      }
    }
  }
  {
    std::lock_guard lock{g_mu};
    if (!g_done) {
      PrintSummary();
    }
  }
  return aether_app->ExitCode();
}
