/*
 * Copyright 2026 Aethernet Inc.
 *
 * Desktop Æther receiver for prepared TX-done diagnostics (TxDiagPayload 0xD6)
 * and deep-sleep E2E (DsPayload 0xD5). Deduplicates by record_id; appends TSV.
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
  std::uint8_t diag_mode{0};
  std::uint8_t tx_cb_total{0};
  std::uint8_t tx_cb_success{0};
  std::uint8_t tx_cb_failed{0};
  std::uint8_t first_status{0xff};
  std::uint32_t first_cb_delta_us{0xffffffffu};
  std::uint32_t first_success_delta_us{0xffffffffu};
  std::uint32_t first_failed_delta_us{0xffffffffu};
  std::uint32_t last_cb_delta_us{0xffffffffu};
  std::uint8_t callbacks_after_success{0};
  std::int8_t rssi{0};
  std::uint8_t disconnect_count{0};
  std::uint8_t last_disconnect_reason{0};
  std::uint8_t reconnect_count{0};
  std::uint8_t ap_primary{0};
  std::uint16_t seq{0};
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

std::filesystem::path TsvPath() {
#if defined(_WIN32)
  if (char const* env = std::getenv("AE_DS_TSV")) {
    return std::filesystem::path{env};
  }
#endif
  return std::filesystem::path{"prepared_tx_done_diag.tsv"};
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
         "cb_seen\tcb_timeout\tbrownout\tauth\tseq\tdiag_mode\ttx_cb_total\t"
         "tx_cb_success\ttx_cb_failed\tfirst_status\tfirst_cb_delta_us\t"
         "first_success_delta_us\tfirst_failed_delta_us\tlast_cb_delta_us\t"
         "callbacks_after_success\trssi\tdisconnect_count\t"
         "last_disconnect_reason\treconnect_count\tap_primary\n";
}

void AppendTsv(Meas const& m) {
  EnsureTsvHeader();
  std::ofstream out(TsvPath(), std::ios::app);
  out << m.record_id << '\t' << static_cast<int>(m.kind) << '\t'
      << static_cast<int>(m.outer) << '\t' << static_cast<int>(m.hot) << '\t'
      << m.user_us << '\t' << m.wifi_us << '\t' << m.connect_us << '\t'
      << m.txdone_us << '\t' << m.teardown_us << '\t' << m.sleep_elapsed_us
      << '\t' << m.sleep_overhead_us << '\t' << m.app_entry_us << '\t'
      << static_cast<int>(m.cb_seen) << '\t' << static_cast<int>(m.cb_timeout)
      << '\t' << static_cast<int>(m.brownout) << '\t'
      << static_cast<int>(m.auth) << '\t' << m.seq << '\t'
      << static_cast<int>(m.diag_mode) << '\t'
      << static_cast<int>(m.tx_cb_total) << '\t'
      << static_cast<int>(m.tx_cb_success) << '\t'
      << static_cast<int>(m.tx_cb_failed) << '\t'
      << static_cast<int>(m.first_status) << '\t' << m.first_cb_delta_us << '\t'
      << m.first_success_delta_us << '\t' << m.first_failed_delta_us << '\t'
      << m.last_cb_delta_us << '\t'
      << static_cast<int>(m.callbacks_after_success) << '\t'
      << static_cast<int>(m.rssi) << '\t'
      << static_cast<int>(m.disconnect_count) << '\t'
      << static_cast<int>(m.last_disconnect_reason) << '\t'
      << static_cast<int>(m.reconnect_count) << '\t'
      << static_cast<int>(m.ap_primary) << '\n';
}

void NoteRecord(Meas m) {
  if (m.record_id == 0 || m.kind == 0) {
    return;
  }
  if (g_seen_records.count(m.record_id)) {
    ++g_dup_records;
    return;
  }
  g_seen_records.insert(m.record_id);
  if (static_cast<int>(m.record_id) < g_max_record) {
    ++g_ooo;
  }
  if (static_cast<int>(m.record_id) > g_max_record) {
    g_max_record = m.record_id;
  }
  if (m.brownout) {
    ++g_brownout_boots;
  }
  g_meas.push_back(m);
  AppendTsv(m);
}

void PrintFinalStats(char const* tag) {
  std::vector<std::uint32_t> full_user;
  std::vector<std::uint32_t> hot_user;
  std::vector<std::uint32_t> hot_wifi;
  std::vector<std::uint32_t> connect;
  std::vector<std::uint32_t> txdone;
  std::vector<std::uint32_t> first_success;
  std::vector<std::uint32_t> first_cb;
  std::vector<std::uint32_t> last_cb;
  std::vector<std::uint32_t> rssi_pos;
  int cb = 0;
  int to = 0;
  int first_ok = 0;
  int first_fail = 0;
  int cb_total_sum = 0;
  int cb_succ_sum = 0;
  int cb_fail_sum = 0;
  int after_succ_sum = 0;
  int fail_before_succ = 0;
  for (auto const& m : g_meas) {
    if (m.kind == static_cast<std::uint8_t>(
                      temp_sensor::bench::DsPendingKind::kFull)) {
      full_user.push_back(m.user_us);
    }
    if (m.kind == static_cast<std::uint8_t>(
                      temp_sensor::bench::DsPendingKind::kHot)) {
      hot_user.push_back(m.user_us);
      hot_wifi.push_back(m.wifi_us);
      connect.push_back(m.connect_us);
      txdone.push_back(m.txdone_us);
      cb += m.cb_seen;
      to += m.cb_timeout;
      cb_total_sum += m.tx_cb_total;
      cb_succ_sum += m.tx_cb_success;
      cb_fail_sum += m.tx_cb_failed;
      after_succ_sum += m.callbacks_after_success;
      if (m.first_status == 1) {
        ++first_ok;
      } else if (m.first_status == 0) {
        ++first_fail;
      }
      if (m.tx_cb_failed > 0 && m.tx_cb_success > 0) {
        ++fail_before_succ;
      }
      if (m.first_cb_delta_us != 0xffffffffu) {
        first_cb.push_back(m.first_cb_delta_us);
      }
      if (m.first_success_delta_us != 0xffffffffu) {
        first_success.push_back(m.first_success_delta_us);
      }
      if (m.last_cb_delta_us != 0xffffffffu) {
        last_cb.push_back(m.last_cb_delta_us);
      }
      rssi_pos.push_back(static_cast<std::uint32_t>(
          static_cast<std::int32_t>(m.rssi) + 200));
    }
  }
  auto rssi_med = [&]() -> int {
    if (rssi_pos.empty()) {
      return 0;
    }
    return static_cast<int>(PercentileUs(rssi_pos, 50)) - 200;
  };
  std::cout << "TEST_RESULT"
            << " full_recv=" << g_full_recv << " hot_recv=" << g_hot_recv
            << " final_recv=" << g_final_recv << " records=" << g_meas.size()
            << " dup=" << g_dup_records << " ooo=" << g_ooo
            << " full_med_ms=" << (PercentileUs(full_user, 50) / 1000)
            << " hot_user_med_ms=" << (PercentileUs(hot_user, 50) / 1000)
            << " hot_wifi_med_ms=" << (PercentileUs(hot_wifi, 50) / 1000)
            << " connect_med_ms=" << (PercentileUs(connect, 50) / 1000)
            << " txdone_med_us=" << PercentileUs(txdone, 50)
            << " first_cb_med_us=" << PercentileUs(first_cb, 50)
            << " first_success_med_us=" << PercentileUs(first_success, 50)
            << " last_cb_med_us=" << PercentileUs(last_cb, 50)
            << " rssi_med=" << rssi_med() << " cb_seen=" << cb
            << " cb_timeout=" << to << " first_status_ok=" << first_ok
            << " first_status_fail=" << first_fail
            << " cb_total_sum=" << cb_total_sum
            << " cb_succ_sum=" << cb_succ_sum << " cb_fail_sum=" << cb_fail_sum
            << " after_succ_sum=" << after_succ_sum
            << " fail_before_succ=" << fail_before_succ
            << " brownout_boots=" << g_brownout_boots << "\n";
  std::cout << "BENCH_DONE " << tag << "\n";
  std::cout.flush();
}

void OnTxDiag(temp_sensor::bench::TxDiagPayload const& p) {
  auto const type = static_cast<temp_sensor::bench::TxDiagMsgType>(p.type);
  if (type == temp_sensor::bench::TxDiagMsgType::kFull) {
    ++g_full_recv;
    std::cout << "FULL_DIAG seq=" << p.sequence_global
              << " reason=" << temp_sensor::bench::FullReasonName(p.full_reason)
              << " reset=" << static_cast<unsigned>(p.reset_reason)
              << " wake=" << static_cast<unsigned>(p.wake_cause)
              << " rtc_state={magic=" << p.rtc_state_magic
              << ",ver=" << p.rtc_state_version
              << ",crc=" << static_cast<unsigned>(p.rtc_state_crc_ok)
              << ",valid=" << static_cast<unsigned>(p.rtc_state_valid) << "}"
              << " phase=" << static_cast<unsigned>(p.phase)
              << " outer=" << static_cast<unsigned>(p.outer_cycle)
              << " hot=" << static_cast<unsigned>(p.hot_index)
              << " prepared={valid="
              << static_cast<unsigned>(p.prepared_block_valid)
              << ",left=" << p.prepared_message_left << "}"
              << " wifi={magic=" << p.rtc_wifi_magic
              << ",ver=" << p.rtc_wifi_version
              << ",crc=" << static_cast<unsigned>(p.rtc_wifi_crc_ok)
              << ",valid=" << static_cast<unsigned>(p.rtc_wifi_valid) << "}"
              << " pre_sleep={phase="
              << static_cast<unsigned>(p.pre_sleep_phase)
              << ",outer=" << static_cast<unsigned>(p.pre_sleep_outer)
              << ",hot=" << static_cast<unsigned>(p.pre_sleep_hot)
              << ",left="
              << static_cast<unsigned>(p.pre_sleep_prepared_left)
              << ",state_crc=" << p.pre_sleep_state_crc
              << ",wifi_crc=" << p.pre_sleep_wifi_crc << "}\n";
  } else if (type == temp_sensor::bench::TxDiagMsgType::kHot) {
    ++g_hot_recv;
    if (g_hot_recv <= 5 || g_hot_recv % 10 == 0) {
      std::cout << ae::Format(
          "RECV HOT outer={} idx={} seq={} record={} user_us={} "
          "cb_t={} cb_s={} cb_f={} first_st={} rssi={}\n",
          p.outer_cycle, p.hot_index, p.sequence_global, p.record_id,
          p.pending_user_cycle_us, p.tx_cb_total, p.tx_cb_success,
          p.tx_cb_failed, p.first_status, p.rssi);
    }
  } else if (type == temp_sensor::bench::TxDiagMsgType::kFinal) {
    ++g_final_recv;
    std::cout << ae::Format("RECV FINAL seq={} record={}\n", p.sequence_global,
                            p.record_id);
  } else {
    std::cout << ae::Format("RECV OTHER type={} seq={}\n", p.type,
                            p.sequence_global);
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
                             temp_sensor::bench::TxDiagFlags::kCallbackSeen))
                  ? 1
                  : 0;
  m.cb_timeout = p.cb_timeout;
  m.brownout =
      (p.flags & static_cast<std::uint8_t>(temp_sensor::bench::TxDiagFlags::kBrownout))
          ? 1
          : 0;
  m.auth = p.negotiated_auth;
  m.diag_mode = p.diag_mode;
  m.tx_cb_total = p.tx_cb_total;
  m.tx_cb_success = p.tx_cb_success;
  m.tx_cb_failed = p.tx_cb_failed;
  m.first_status = p.first_status;
  m.first_cb_delta_us = p.first_cb_delta_us;
  m.first_success_delta_us = p.first_success_delta_us;
  m.first_failed_delta_us = p.first_failed_delta_us;
  m.last_cb_delta_us = p.last_cb_delta_us;
  m.callbacks_after_success = p.callbacks_after_success;
  m.rssi = p.rssi;
  m.disconnect_count = p.disconnect_count;
  m.last_disconnect_reason = p.last_disconnect_reason;
  m.reconnect_count = p.reconnect_count;
  m.ap_primary = p.ap_primary;
  m.seq = p.sequence_global;
  NoteRecord(m);

  if (type == temp_sensor::bench::TxDiagMsgType::kFinal) {
    PrintFinalStats("tx_done_diag");
  }
  std::cout.flush();
}

void OnDs(temp_sensor::bench::DsPayload const& p) {
  auto const type = static_cast<temp_sensor::bench::DsMsgType>(p.type);
  if (type == temp_sensor::bench::DsMsgType::kFull) {
    ++g_full_recv;
  } else if (type == temp_sensor::bench::DsMsgType::kHot) {
    ++g_hot_recv;
  } else if (type == temp_sensor::bench::DsMsgType::kFinal) {
    ++g_final_recv;
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
  m.seq = p.sequence_global;
  NoteRecord(m);
  if (type == temp_sensor::bench::DsMsgType::kFinal) {
    PrintFinalStats("deepsleep_5x50");
  }
}

void OnMessage(ae::Uid, ae::DataBuffer const& data) {
  std::lock_guard lock{g_mu};
  temp_sensor::bench::TxDiagPayload td{};
  if (temp_sensor::bench::DecodeTxDiag(data, td)) {
    OnTxDiag(td);
    return;
  }
  temp_sensor::bench::DsPayload ds{};
  if (temp_sensor::bench::DecodeDs(data, ds)) {
    OnDs(ds);
    return;
  }
  temp_sensor::bench::FastPayload fp{};
  if (temp_sensor::bench::DecodeFast(data, fp)) {
    std::cout << "RECV FAST (ignored) type=" << static_cast<int>(fp.type)
              << "\n";
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
