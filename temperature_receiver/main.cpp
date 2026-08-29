/*
 * Copyright 2026 Aethernet Inc.
 *
 * Desktop Æther receiver for silent fastest-path prepared Wi-Fi campaign.
 * Stays up across firmware reflashes; prints TEST_RESULT after each FINAL.
 */

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <iomanip>
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

struct TestStats {
  int planned{20};
  int delivered{0};
  int duplicates{0};
  int out_of_order{0};
  int max_idx_seen{0};
  std::vector<std::uint8_t> got;
  std::vector<std::uint32_t> cycle_us;
  std::vector<std::uint32_t> connect_us;
  std::vector<std::uint32_t> tx_done_wait_us;
  std::vector<std::uint32_t> teardown_us;
  std::uint16_t wifi_ready{0};
  std::uint16_t encode{0};
  std::uint16_t sendto{0};
  std::uint16_t nonce{0};
  std::uint8_t test_id{0};
  std::uint16_t pre_ms{0};
  std::uint16_t post_ms{0};
  std::uint8_t assoc_bits{0};
  std::uint8_t auth{0};
  std::uint8_t retry_max{0};
  std::uint8_t post_mode{0};
  int cb_any{0};
  int cb_match{0};
  int cb_timeout{0};
};

std::mutex g_mu;
std::vector<std::unique_ptr<ae::P2pStream>> g_streams;
TestStats g_st{};
int g_full_recv = 0;
int g_prep_recv = 0;
int g_final_recv = 0;

std::int64_t NowMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::uint32_t PercentileUs(std::vector<std::uint32_t> v, int pct) {
  if (v.empty()) {
    return 0;
  }
  std::sort(v.begin(), v.end());
  auto const i = static_cast<size_t>((v.size() - 1) * pct / 100);
  return v[i];
}

void ResetStats(std::uint8_t test_id, int planned) {
  g_st = {};
  g_st.test_id = test_id;
  g_st.planned = planned > 0 ? planned : 20;
  g_st.got.assign(static_cast<size_t>(g_st.planned), 0);
}

void NotePrepared(int idx) {
  if (idx < 1) {
    return;
  }
  if (idx > g_st.planned) {
    g_st.planned = idx;
    g_st.got.resize(static_cast<size_t>(g_st.planned), 0);
  }
  auto& seen = g_st.got[static_cast<size_t>(idx - 1)];
  if (!seen) {
    seen = 1;
    ++g_st.delivered;
    if (idx < g_st.max_idx_seen) {
      ++g_st.out_of_order;
    }
    if (idx > g_st.max_idx_seen) {
      g_st.max_idx_seen = idx;
    }
  } else {
    ++g_st.duplicates;
  }
}

void PrintTestResult() {
  auto const cyc_med = PercentileUs(g_st.cycle_us, 50) / 1000;
  auto const cyc_p90 = PercentileUs(g_st.cycle_us, 90) / 1000;
  auto const cyc_max =
      g_st.cycle_us.empty()
          ? 0
          : *std::max_element(g_st.cycle_us.begin(), g_st.cycle_us.end()) /
                1000;
  auto const conn_med = PercentileUs(g_st.connect_us, 50) / 1000;
  auto const txdone_med = PercentileUs(g_st.tx_done_wait_us, 50) / 1000;
  auto const teardown_med = PercentileUs(g_st.teardown_us, 50) / 1000;
  int missing = g_st.planned - g_st.delivered;
  if (missing < 0) {
    missing = 0;
  }
  std::cout << "TEST_RESULT"
            << " test_id=" << static_cast<int>(g_st.test_id)
            << " n=" << g_st.planned << " delivered=" << g_st.delivered << "/"
            << g_st.planned << " connect_med_ms=" << conn_med
            << " cycle_med_ms=" << cyc_med << " p90_ms=" << cyc_p90
            << " max_ms=" << cyc_max
            << " wifi_ready=" << static_cast<int>(g_st.wifi_ready)
            << " encode=" << static_cast<int>(g_st.encode)
            << " sendto=" << static_cast<int>(g_st.sendto)
            << " nonce=" << static_cast<int>(g_st.nonce)
            << " pre=" << g_st.pre_ms << " post=" << g_st.post_ms
            << " assoc=0x" << std::hex << static_cast<int>(g_st.assoc_bits)
            << std::dec << " auth=" << static_cast<int>(g_st.auth)
            << " retry=" << static_cast<int>(g_st.retry_max)
            << " post_mode=" << static_cast<int>(g_st.post_mode)
            << " cb_any=" << g_st.cb_any << " cb_match=" << g_st.cb_match
            << " cb_timeout=" << g_st.cb_timeout
            << " txdone_med_ms=" << txdone_med
            << " teardown_med_ms=" << teardown_med
            << " missing=" << missing
            << " duplicates=" << g_st.duplicates
            << " ooo=" << g_st.out_of_order
            << " samples=" << g_st.cycle_us.size() << "\n";
  std::cout << "BENCH_DONE test_id=" << static_cast<int>(g_st.test_id) << "\n";
  std::cout.flush();
}

void OnFast(temp_sensor::bench::FastPayload const& p) {
  auto const ts = NowMs();
  auto const type = static_cast<temp_sensor::bench::FastMsgType>(p.type);
  if (type == temp_sensor::bench::FastMsgType::kFull) {
    ++g_full_recv;
    int planned = p.prepared_index;
    if (planned == 0) {
      planned = 20;
    }
    ResetStats(p.test_id, planned);
    g_st.pre_ms = p.pre_ms;
    g_st.post_ms = p.post_ms;
    g_st.assoc_bits = p.assoc_bits;
    g_st.retry_max = p.retry_max;
    g_st.post_mode = p.post_mode;
    std::cout << ae::Format("RECV FULL test_id={} n={} seq={} ts={}\n",
                            p.test_id, planned, p.sequence_global, ts);
  } else if (type == temp_sensor::bench::FastMsgType::kPrepared) {
    ++g_prep_recv;
    if (g_st.planned == 0 || g_st.test_id != p.test_id) {
      // New test without FULL, or FULL was lost — start a fresh window.
      int planned = p.prepared_index > 0 ? static_cast<int>(p.prepared_index) : 20;
      // prepared_index is 1-based send index, not N; keep previous planned if
      // same test, otherwise default to at least the index we just saw.
      if (g_st.test_id != p.test_id || g_st.planned == 0) {
        planned = 20;
        if (p.prepared_index > planned) {
          planned = p.prepared_index;
        }
        ResetStats(p.test_id, planned);
      }
    }
    NotePrepared(p.prepared_index);
    g_st.pre_ms = p.pre_ms;
    g_st.post_ms = p.post_ms;
    g_st.assoc_bits = p.assoc_bits;
    g_st.auth = p.auth_negotiated;
    g_st.retry_max = p.retry_max;
    g_st.post_mode = p.post_mode;
    g_st.cb_any += p.cb_any;
    g_st.cb_match += p.cb_match;
    g_st.cb_timeout += p.cb_timeout;
    if (p.cycle_us != 0) {
      g_st.cycle_us.push_back(p.cycle_us);
    }
    if (p.connect_us != 0) {
      g_st.connect_us.push_back(p.connect_us);
    }
    if (p.tx_done_wait_us != 0 || p.cb_any || p.cb_timeout) {
      g_st.tx_done_wait_us.push_back(p.tx_done_wait_us);
    }
    if (p.teardown_us != 0) {
      g_st.teardown_us.push_back(p.teardown_us);
    }
    std::cout << ae::Format(
        "RECV PREPARED test_id={} idx={} seq={} cycle_us={} connect_us={} "
        "txdone_us={} teardown_us={} auth={} cb={} to={} flags={} ts={}\n",
        p.test_id, p.prepared_index, p.sequence_global, p.cycle_us,
        p.connect_us, p.tx_done_wait_us, p.teardown_us, p.auth_negotiated,
        p.cb_any, p.cb_timeout, p.status_flags, ts);
  } else if (type == temp_sensor::bench::FastMsgType::kFinal) {
    ++g_final_recv;
    g_st.test_id = p.test_id;
    if (p.prepared_index != 0) {
      g_st.planned = p.prepared_index;
    } else if (p.wifi_ready_count != 0) {
      g_st.planned = p.wifi_ready_count;
    }
    g_st.wifi_ready = p.wifi_ready_count;
    g_st.encode = p.encode_count;
    g_st.sendto = p.sendto_count;
    g_st.nonce = p.nonce_consumed;
    g_st.auth = p.auth_negotiated;
    g_st.pre_ms = p.pre_ms;
    g_st.post_ms = p.post_ms;
    g_st.assoc_bits = p.assoc_bits;
    g_st.retry_max = p.retry_max;
    g_st.post_mode = p.post_mode;
    // FINAL carries device totals for callback_seen / timeout.
    g_st.cb_any = p.cb_any;
    g_st.cb_match = p.cb_match;
    g_st.cb_timeout = p.cb_timeout;
    if (p.cycle_us != 0) {
      g_st.cycle_us.push_back(p.cycle_us);
    }
    if (p.connect_us != 0) {
      g_st.connect_us.push_back(p.connect_us);
    }
    if (p.tx_done_wait_us != 0 || p.cb_any || p.cb_timeout) {
      g_st.tx_done_wait_us.push_back(p.tx_done_wait_us);
    }
    if (p.teardown_us != 0) {
      g_st.teardown_us.push_back(p.teardown_us);
    }
    // Prefer device counters for delivery when FULL was missed.
    if (g_st.delivered == 0 && p.sendto_count != 0) {
      g_st.delivered = p.sendto_count;
    }
    std::cout << ae::Format(
        "RECV FINAL test_id={} seq={} last_cycle={} wifi_ready={} encode={} "
        "sendto={} nonce={} ts={}\n",
        p.test_id, p.sequence_global, p.cycle_us, p.wifi_ready_count,
        p.encode_count, p.sendto_count, p.nonce_consumed, ts);
    PrintTestResult();
  }
  std::cout.flush();
}

void OnMessage(ae::Uid sender, ae::DataBuffer const& data) {
  std::lock_guard lock{g_mu};
  temp_sensor::bench::FastPayload fp{};
  if (temp_sensor::bench::DecodeFast(data, fp)) {
    OnFast(fp);
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

  while (!aether_app->IsExited()) {
    auto t = aether_app->Update(ae::Now());
    aether_app->WaitUntil(t);
  }
  return aether_app->ExitCode();
}
