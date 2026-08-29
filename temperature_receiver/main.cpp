/*
 * Copyright 2026 Aethernet Inc.
 *
 * Desktop Æther receiver for prepared Wi-Fi single-factor bisect.
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
// Reuse the stable cache-bench receiver identity (UID 5aade50f-...).
static constexpr char const* kClientName = "prepared_wifi_cache_rx_v1";
static constexpr int kVariants =
    static_cast<int>(temp_sensor::bench::BisectVariant::kCount);
static constexpr int kPreparedPer = 20;

struct VariantStats {
  int delivered{0};
  int duplicates{0};
  std::array<bool, kPreparedPer> got{};
  std::array<bool, kPreparedPer> have_us{};
  std::array<std::uint32_t, kPreparedPer> us{};
  std::array<std::uint8_t, kPreparedPer> req_ch{};
  std::array<std::uint8_t, kPreparedPer> act_ch{};
  std::uint8_t wifi_ready{0};
  std::uint8_t encode{0};
  std::uint8_t sendto{0};
  std::uint8_t nonce{0};
  bool have_summary{false};
  bool have_meta{false};
  std::uint8_t cached_channel{0};
  std::uint32_t cached_ip{0};
  std::uint8_t pre_delay_ms{0};
  int channel_match{0};
  int channel_mismatch{0};
};

std::mutex g_mu;
std::vector<std::unique_ptr<ae::P2pStream>> g_streams;
std::array<VariantStats, kVariants> g_var{};
std::vector<std::uint16_t> g_seen_seq;
int g_last_seq = 0;
int g_out_of_order = 0;
int g_full_recv = 0;
int g_meta_recv = 0;
int g_prep_recv = 0;
int g_final_recv = 0;
bool g_done = false;

std::int64_t NowMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::uint32_t MedianUs(std::vector<std::uint32_t> v) {
  if (v.empty()) {
    return 0;
  }
  std::sort(v.begin(), v.end());
  return v[v.size() / 2];
}

char const* Verdict(int delivered, int wifi_ready) {
  if (wifi_ready > 0 && wifi_ready < 10) {
    return "INCONCLUSIVE";
  }
  if (delivered >= 18) {
    return "OK";
  }
  if (delivered >= 10) {
    return "DEGRADES";
  }
  return "BREAKS";
}

void PrintSummary() {
  std::cout << "BISECT_TABLE\n";
  std::cout << "Variant\tSingle change\tDelivered/20\tMissing\tMedian_ms\t"
               "WifiReady\tEncode\tSendto\tNonce\tVerdict\n";
  for (int v = 0; v < kVariants; ++v) {
    auto const& s = g_var[static_cast<size_t>(v)];
    int missing = kPreparedPer - s.delivered;
    if (missing < 0) {
      missing = 0;
    }
    std::vector<std::uint32_t> times;
    for (int i = 0; i < kPreparedPer; ++i) {
      if (s.have_us[static_cast<size_t>(i)]) {
        times.push_back(s.us[static_cast<size_t>(i)]);
      }
    }
    auto const med_ms = MedianUs(times) / 1000;
    std::cout << temp_sensor::bench::BisectVariantName(
                     static_cast<std::uint8_t>(v))
              << '\t'
              << temp_sensor::bench::BisectVariantChange(
                     static_cast<std::uint8_t>(v))
              << '\t' << s.delivered << "/20\t" << missing << '\t' << med_ms
              << '\t' << static_cast<int>(s.wifi_ready) << '\t'
              << static_cast<int>(s.encode) << '\t'
              << static_cast<int>(s.sendto) << '\t'
              << static_cast<int>(s.nonce) << '\t'
              << Verdict(s.delivered, s.wifi_ready) << '\n';
  }

  auto get = [](int id) {
    return g_var[static_cast<size_t>(id)].delivered;
  };
  std::cout << "CHANNEL_HYPOTHESIS\n";
  std::cout << "B1 no cache = " << get(1) << "/20\n";
  std::cout << "C1 BSSID only = " << get(2) << "/20\n";
  std::cout << "C2 channel only = " << get(3) << "/20\n";
  std::cout << "C3 BSSID+channel = " << get(4) << "/20\n";
  std::cout << "C7 BSSID+static IP = " << get(8) << "/20\n";
  std::cout << "C8 channel+static IP = " << get(9) << "/20\n";

  bool channel_bad = false;
  bool channel_ok = false;
  // Correlate: variants that set channel (C2,C3,C8) vs without (B1,C1,C7)
  auto const with_ch = get(3) + get(4) + get(9);
  auto const without_ch = get(1) + get(2) + get(8);
  if (without_ch - with_ch >= 15) {
    channel_bad = true;
  }
  if (with_ch >= without_ch - 3) {
    channel_ok = true;
  }
  std::cout << "Does cached channel independently correlate with loss? ";
  if (channel_bad && !channel_ok) {
    std::cout << "YES\n";
  } else if (!channel_bad && channel_ok) {
    std::cout << "NO\n";
  } else {
    std::cout << "INCONCLUSIVE\n";
  }

  std::cout << "CHANNEL_MATCH_COUNTS\n";
  for (int v : {3, 4, 9}) {
    auto const& s = g_var[static_cast<size_t>(v)];
    std::cout << temp_sensor::bench::BisectVariantName(
                     static_cast<std::uint8_t>(v))
              << " match=" << s.channel_match
              << " mismatch=" << s.channel_mismatch
              << " cached_ch=" << static_cast<int>(s.cached_channel) << '\n';
  }

  std::cout << "DELIVERY_TOTALS\n";
  std::cout << "  full=" << g_full_recv << "/" << kVariants << "\n";
  std::cout << "  meta=" << g_meta_recv << "/" << kVariants << "\n";
  std::cout << "  prepared=" << g_prep_recv << "/" << (kVariants * kPreparedPer)
            << "\n";
  std::cout << "  final=" << g_final_recv << "/1\n";
  std::cout << "  out_of_order=" << g_out_of_order << "\n";
  std::cout << "BENCH_DONE\n";
  std::cout.flush();
}

void NoteSeq(std::uint16_t seq) {
  if (std::find(g_seen_seq.begin(), g_seen_seq.end(), seq) !=
      g_seen_seq.end()) {
    return;
  }
  g_seen_seq.push_back(seq);
  if (seq != 0 && g_last_seq != 0 &&
      seq < static_cast<std::uint16_t>(g_last_seq)) {
    ++g_out_of_order;
  }
  g_last_seq = seq;
}

void ApplyPreparedMetrics(int v, int slot,
                          temp_sensor::bench::BisectPayload const& p) {
  if (v < 0 || v >= kVariants || slot < 0 || slot >= kPreparedPer) {
    return;
  }
  auto& s = g_var[static_cast<size_t>(v)];
  if (p.time_us != 0) {
    s.us[static_cast<size_t>(slot)] = p.time_us;
    s.have_us[static_cast<size_t>(slot)] = true;
  }
  s.req_ch[static_cast<size_t>(slot)] = p.requested_channel;
  s.act_ch[static_cast<size_t>(slot)] = p.actual_channel;
  if (p.requested_channel != 0) {
    if (p.requested_channel == p.actual_channel) {
      ++s.channel_match;
    } else if (p.actual_channel != 0) {
      ++s.channel_mismatch;
    }
  }
}

void OnBisect(temp_sensor::bench::BisectPayload const& p) {
  auto const ts = NowMs();
  NoteSeq(p.sequence_global);
  int const v = static_cast<int>(p.variant_id);
  auto type = static_cast<temp_sensor::bench::BisectMsgType>(p.type);

  if (type == temp_sensor::bench::BisectMsgType::kFull) {
    ++g_full_recv;
    if (v >= 1 && v < kVariants) {
      // Previous variant summary rides on this FULL.
      auto& prev = g_var[static_cast<size_t>(v - 1)];
      prev.wifi_ready = p.wifi_ready_count;
      prev.encode = p.encode_count;
      prev.sendto = p.sendto_count;
      prev.nonce = p.nonce_consumed;
      prev.have_summary = true;
    }
    std::cout << ae::Format(
        "RECV FULL variant={} seq={} time_us={} ts={}\n",
        temp_sensor::bench::BisectVariantName(p.variant_id), p.sequence_global,
        p.time_us, ts);
  } else if (type == temp_sensor::bench::BisectMsgType::kMeta) {
    ++g_meta_recv;
    if (v >= 0 && v < kVariants) {
      auto& s = g_var[static_cast<size_t>(v)];
      s.have_meta = true;
      s.cached_channel = p.cached_channel;
      s.cached_ip = p.cached_ip;
      s.pre_delay_ms = p.pre_delay_ms;
    }
    std::cout << ae::Format(
        "RECV META variant={} seq={} cached_ch={} cached_ip={:08x} pre_ms={} "
        "ts={}\n",
        temp_sensor::bench::BisectVariantName(p.variant_id), p.sequence_global,
        p.cached_channel, p.cached_ip, p.pre_delay_ms, ts);
  } else if (type == temp_sensor::bench::BisectMsgType::kPrepared) {
    ++g_prep_recv;
    if (v >= 0 && v < kVariants) {
      auto& s = g_var[static_cast<size_t>(v)];
      int const idx = static_cast<int>(p.prepared_index);
      if (idx == 1 && p.cached_channel != 0) {
        s.have_meta = true;
        s.cached_channel = p.cached_channel;
        s.cached_ip = p.cached_ip;
        s.pre_delay_ms = p.pre_delay_ms;
      }
      if (idx >= 1 && idx <= kPreparedPer) {
        auto& seen = s.got[static_cast<size_t>(idx - 1)];
        if (!seen) {
          seen = true;
          ++s.delivered;
        } else {
          ++s.duplicates;
        }
      }
      if (idx >= 2) {
        ApplyPreparedMetrics(v, idx - 2, p);
      }
    }
    std::cout << ae::Format(
        "RECV PREPARED variant={} idx={} seq={} prev_us={} req_ch={} act_ch={} "
        "flags={} ts={}\n",
        temp_sensor::bench::BisectVariantName(p.variant_id), p.prepared_index,
        p.sequence_global, p.time_us, p.requested_channel, p.actual_channel,
        p.status_flags, ts);
  } else if (type == temp_sensor::bench::BisectMsgType::kFinal) {
    ++g_final_recv;
    if (v >= 0 && v < kVariants) {
      auto& s = g_var[static_cast<size_t>(v)];
      s.wifi_ready = p.wifi_ready_count;
      s.encode = p.encode_count;
      s.sendto = p.sendto_count;
      s.nonce = p.nonce_consumed;
      s.have_summary = true;
      ApplyPreparedMetrics(v, kPreparedPer - 1, p);
    }
    std::cout << ae::Format(
        "RECV FINAL variant={} seq={} last_us={} wifi_ready={} encode={} "
        "sendto={} nonce={} ts={}\n",
        temp_sensor::bench::BisectVariantName(p.variant_id), p.sequence_global,
        p.time_us, p.wifi_ready_count, p.encode_count, p.sendto_count,
        p.nonce_consumed, ts);
    PrintSummary();
    g_done = true;
  }
  std::cout.flush();
}

void OnMessage(ae::Uid sender, ae::DataBuffer const& data) {
  std::lock_guard lock{g_mu};
  temp_sensor::bench::BisectPayload bp{};
  if (temp_sensor::bench::DecodeBisect(data, bp)) {
    OnBisect(bp);
    return;
  }
  std::cout << "RECV unknown sender=" << ae::Format("{}", sender)
            << " size=" << data.size() << "\n";
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
              g_streams.push_back(std::move(stream));
            });
      });

  while (!aether_app->IsExited() && !g_done) {
    auto t = aether_app->Update(ae::Now());
    aether_app->WaitUntil(t);
  }
  return aether_app->ExitCode();
}
