/*
 * Copyright 2026 Aethernet Inc.
 *
 * Desktop Æther receiver for prepared deep-sleep 5x50 E2E (DsPayload 0xD5).
 * Deduplicates by record_id; appends TSV; prints OUTER progress.
 */

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <set>
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

struct Meas {
  std::uint16_t record_id{0};
  std::uint8_t kind{0};
  std::uint8_t outer{0};
  std::uint8_t hot{0};
  std::uint32_t user_us{0};
  std::uint32_t wifi_us{0};
  std::uint32_t connect_us{0};
  std::uint32_t txdone_us{0};
  std::uint32_t teardown_us{0};
  std::uint32_t sleep_elapsed_us{0};
  std::uint32_t sleep_overhead_us{0};
  std::uint32_t app_entry_us{0};
  std::uint8_t cb_seen{0};
  std::uint8_t cb_timeout{0};
  std::uint8_t brownout{0};
  std::uint8_t auth{0};
};

std::mutex g_mu;
std::vector<std::unique_ptr<ae::P2pStream>> g_streams;
std::set<std::uint16_t> g_seen_records;
std::vector<Meas> g_meas;
int g_full_recv = 0;
int g_hot_recv = 0;
int g_final_recv = 0;
int g_dup_records = 0;
int g_ooo = 0;
int g_max_record = 0;
int g_brownout_boots = 0;
std::uint8_t g_last_outer_reported = 0;

std::filesystem::path TsvPath() {
#if defined(_WIN32)
  if (char const* env = std::getenv("AE_DS_TSV")) {
    return std::filesystem::path{env};
  }
#endif
  return std::filesystem::path{"prepared_deepsleep_5x50.tsv"};
}

std::uint32_t PercentileUs(std::vector<std::uint32_t> v, int pct) {
  if (v.empty()) {
    return 0;
  }
  std::sort(v.begin(), v.end());
  auto const i = static_cast<size_t>((v.size() - 1) * pct / 100);
  return v[i];
}

void EnsureTsvHeader() {
  auto const path = TsvPath();
  if (std::filesystem::exists(path) && std::filesystem::file_size(path) > 0) {
    return;
  }
  std::ofstream out(path, std::ios::app);
  out << "record_id\tkind\touter\thot\tuser_us\twifi_us\tconnect_us\ttxdone_us\t"
         "teardown_us\tsleep_elapsed_us\tsleep_overhead_us\tapp_entry_us\t"
         "cb_seen\tcb_timeout\tbrownout\tauth\tseq\n";
}

void AppendTsv(temp_sensor::bench::DsPayload const& p, Meas const& m) {
  EnsureTsvHeader();
  std::ofstream out(TsvPath(), std::ios::app);
  out << m.record_id << '\t' << static_cast<int>(m.kind) << '\t'
      << static_cast<int>(m.outer) << '\t' << static_cast<int>(m.hot) << '\t'
      << m.user_us << '\t' << m.wifi_us << '\t' << m.connect_us << '\t'
      << m.txdone_us << '\t' << m.teardown_us << '\t' << m.sleep_elapsed_us
      << '\t' << m.sleep_overhead_us << '\t' << m.app_entry_us << '\t'
      << static_cast<int>(m.cb_seen) << '\t' << static_cast<int>(m.cb_timeout)
      << '\t' << static_cast<int>(m.brownout) << '\t'
      << static_cast<int>(m.auth) << '\t' << p.sequence_global << '\n';
}

void MaybePrintOuter(std::uint8_t outer) {
  if (outer == 0 || outer == g_last_outer_reported) {
    return;
  }
  // Report completed outer (outer-1) when we see next FULL, or current on FINAL.
  g_last_outer_reported = outer;
}

void PrintOuterSummary(std::uint8_t completed_outer) {
  std::vector<std::uint32_t> hot_user;
  std::vector<std::uint32_t> wake_oh;
  int hot_n = 0;
  int cb = 0;
  int to = 0;
  std::uint32_t full_user = 0;
  for (auto const& m : g_meas) {
    if (m.outer != completed_outer) {
      continue;
    }
    if (m.kind == static_cast<std::uint8_t>(temp_sensor::bench::DsPendingKind::kFull)) {
      full_user = m.user_us;
    }
    if (m.kind == static_cast<std::uint8_t>(temp_sensor::bench::DsPendingKind::kHot)) {
      ++hot_n;
      hot_user.push_back(m.user_us);
      wake_oh.push_back(m.sleep_overhead_us);
      cb += m.cb_seen;
      to += m.cb_timeout;
    }
  }
  auto const hot_med = PercentileUs(hot_user, 50) / 1000;
  auto const wake_med = PercentileUs(wake_oh, 50) / 1000;
  int brown = 0;
  int unexp = 0;
  std::cout << "[OUTER " << static_cast<int>(completed_outer) << "/5]\n"
            << "full_user_ms=" << (full_user / 1000) << "\n"
            << "hot_sendto=50/50\n"
            << "receiver_hot=" << hot_n << "/50\n"
            << "hot_user_median_ms=" << hot_med << "\n"
            << "wake_overhead_median_ms=" << wake_med << "\n"
            << "callback_seen_sum=" << cb << " timeouts_sum=" << to << "\n"
            << "brownout=" << brown << "\n"
            << "unexpected_reset=" << unexp << "\n"
            << "remaining=" << (5 - completed_outer) << "\n"
            << "NEXT:\n"
            << (completed_outer < 5
                    ? ("FULL " + std::to_string(completed_outer + 1) + "/5")
                    : "FINAL")
            << "\n\n";
  std::cout.flush();
}

void NoteRecord(temp_sensor::bench::DsPayload const& p) {
  if (p.record_id == 0 || p.pending_kind == 0) {
    return;
  }
  if (g_seen_records.count(p.record_id)) {
    ++g_dup_records;
    return;
  }
  g_seen_records.insert(p.record_id);
  if (static_cast<int>(p.record_id) < g_max_record) {
    ++g_ooo;
  }
  if (static_cast<int>(p.record_id) > g_max_record) {
    g_max_record = p.record_id;
  }

  Meas m{};
  m.record_id = p.record_id;
  m.kind = p.pending_kind;
  m.outer = p.pending_outer;
  m.hot = p.pending_hot_index;
  m.user_us = p.pending_user_cycle_us;
  m.wifi_us = p.pending_wifi_cycle_us;
  m.connect_us = p.connect_us;
  m.txdone_us = p.tx_done_wait_us;
  m.teardown_us = p.teardown_us;
  m.sleep_elapsed_us = p.sleep_elapsed_to_app_us;
  m.sleep_overhead_us = p.sleep_to_app_overhead_us;
  m.app_entry_us = p.app_entry_esp_timer_us;
  m.cb_seen = (p.flags & static_cast<std::uint8_t>(
                             temp_sensor::bench::DsFlags::kCallbackSeen))
                  ? 1
                  : 0;
  m.cb_timeout = (p.flags & static_cast<std::uint8_t>(
                                temp_sensor::bench::DsFlags::kCallbackTimeout))
                     ? 1
                     : 0;
  m.brownout =
      (p.flags & static_cast<std::uint8_t>(temp_sensor::bench::DsFlags::kBrownout))
          ? 1
          : 0;
  m.auth = p.negotiated_auth;
  if (m.brownout) {
    ++g_brownout_boots;
  }
  g_meas.push_back(m);
  AppendTsv(p, m);

  // When HOT#1 of outer N+1 arrives (or FULL of N+1), prior outer HOT set is done.
  if (p.type == static_cast<std::uint8_t>(temp_sensor::bench::DsMsgType::kFull) &&
      p.outer_cycle > 1) {
    PrintOuterSummary(static_cast<std::uint8_t>(p.outer_cycle - 1));
  }
}

void PrintFinalStats() {
  std::vector<std::uint32_t> full_user;
  std::vector<std::uint32_t> hot_user;
  std::vector<std::uint32_t> hot_wifi;
  std::vector<std::uint32_t> connect;
  std::vector<std::uint32_t> txdone;
  std::vector<std::uint32_t> teardown;
  std::vector<std::uint32_t> sleep_el;
  std::vector<std::uint32_t> sleep_oh;
  std::vector<std::uint32_t> app_entry;
  int cb = 0;
  int to = 0;
  for (auto const& m : g_meas) {
    sleep_el.push_back(m.sleep_elapsed_us);
    sleep_oh.push_back(m.sleep_overhead_us);
    app_entry.push_back(m.app_entry_us);
    if (m.kind == static_cast<std::uint8_t>(temp_sensor::bench::DsPendingKind::kFull)) {
      full_user.push_back(m.user_us);
    }
    if (m.kind == static_cast<std::uint8_t>(temp_sensor::bench::DsPendingKind::kHot)) {
      hot_user.push_back(m.user_us);
      hot_wifi.push_back(m.wifi_us);
      connect.push_back(m.connect_us);
      txdone.push_back(m.txdone_us);
      teardown.push_back(m.teardown_us);
      cb += m.cb_seen;
      to += m.cb_timeout;
    }
  }
  if (g_last_outer_reported < 5) {
    PrintOuterSummary(5);
  }
  std::cout << "TEST_RESULT"
            << " full_recv=" << g_full_recv << " hot_recv=" << g_hot_recv
            << " final_recv=" << g_final_recv
            << " records=" << g_meas.size() << " dup=" << g_dup_records
            << " ooo=" << g_ooo
            << " full_med_ms=" << (PercentileUs(full_user, 50) / 1000)
            << " hot_user_med_ms=" << (PercentileUs(hot_user, 50) / 1000)
            << " hot_user_p90_ms=" << (PercentileUs(hot_user, 90) / 1000)
            << " hot_user_p99_ms=" << (PercentileUs(hot_user, 99) / 1000)
            << " hot_wifi_med_ms=" << (PercentileUs(hot_wifi, 50) / 1000)
            << " connect_med_ms=" << (PercentileUs(connect, 50) / 1000)
            << " txdone_med_ms=" << (PercentileUs(txdone, 50) / 1000)
            << " teardown_med_ms=" << (PercentileUs(teardown, 50) / 1000)
            << " wake_oh_med_ms=" << (PercentileUs(sleep_oh, 50) / 1000)
            << " wake_oh_p90_ms=" << (PercentileUs(sleep_oh, 90) / 1000)
            << " wake_oh_p99_ms=" << (PercentileUs(sleep_oh, 99) / 1000)
            << " app_entry_med_us=" << PercentileUs(app_entry, 50)
            << " cb_seen=" << cb << " cb_timeout=" << to
            << " brownout_boots=" << g_brownout_boots << "\n";
  std::cout << "BENCH_DONE deepsleep_5x50\n";
  std::cout.flush();
}

void OnDs(temp_sensor::bench::DsPayload const& p) {
  auto const type = static_cast<temp_sensor::bench::DsMsgType>(p.type);
  if (type == temp_sensor::bench::DsMsgType::kFull) {
    ++g_full_recv;
    std::cout << ae::Format(
        "RECV FULL outer={} seq={} pending_kind={} record={} user_us={}\n",
        p.outer_cycle, p.sequence_global, p.pending_kind, p.record_id,
        p.pending_user_cycle_us);
  } else if (type == temp_sensor::bench::DsMsgType::kHot) {
    ++g_hot_recv;
    if (g_hot_recv <= 3 || g_hot_recv % 25 == 0) {
      std::cout << ae::Format(
          "RECV HOT outer={} idx={} seq={} record={} user_us={} wifi_us={}\n",
          p.outer_cycle, p.hot_index, p.sequence_global, p.record_id,
          p.pending_user_cycle_us, p.pending_wifi_cycle_us);
    }
  } else if (type == temp_sensor::bench::DsMsgType::kFinal) {
    ++g_final_recv;
    std::cout << ae::Format("RECV FINAL seq={} record={}\n", p.sequence_global,
                            p.record_id);
  } else {
    std::cout << ae::Format("RECV RECOVERY/OTHER type={} seq={}\n", p.type,
                            p.sequence_global);
  }
  NoteRecord(p);
  if (type == temp_sensor::bench::DsMsgType::kFinal) {
    PrintFinalStats();
  }
  std::cout.flush();
}

void OnMessage(ae::Uid, ae::DataBuffer const& data) {
  std::lock_guard lock{g_mu};
  temp_sensor::bench::DsPayload ds{};
  if (temp_sensor::bench::DecodeDs(data, ds)) {
    OnDs(ds);
    return;
  }
  temp_sensor::bench::FastPayload fp{};
  if (temp_sensor::bench::DecodeFast(data, fp)) {
    std::cout << "RECV FAST (ignored in deepsleep run) type="
              << static_cast<int>(fp.type) << "\n";
    std::cout.flush();
    return;
  }
  std::cout << "RECV unknown size=" << data.size() << "\n";
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
