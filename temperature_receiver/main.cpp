/*
 * Copyright 2026 Aethernet Inc.
 *
 * Desktop Æther receiver for prepared boot/wifi opt (0xD8), MAC-retry (0xD7),
 * TX-done (0xD6), deep-sleep E2E (0xD5), nosleep reliability (0xD9).
 * Deduplicates by record_id; appends TSV.
 */

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
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
#include "aether/cloud_connections/cloud_server_connection.h"
#include "aether/types/address.h"
#include "bench_payload.h"

using namespace std::chrono_literals;

namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kClientName = "prepared_wifi_cache_rx_v1";

std::filesystem::path ResolveSessionRoot();

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
  std::uint8_t variant{0};
  std::uint8_t short_retry{0};
  std::uint8_t long_retry{0};
  std::uint8_t retry_called{0};
  std::int16_t retry_set_rc{-1};
  std::uint32_t retry_cfg_us{0};
  std::uint32_t encode_us{0};
  std::uint8_t actual_channel{0};
  std::uint32_t wifi_init_us{0};
  std::uint32_t heap_before{0};
  std::uint32_t heap_after{0};
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
int g_failed_assoc_wakes = 0;
int g_tcp_links_up = 0;
bool g_rx_tcp_verified = false;
std::chrono::steady_clock::time_point g_last_udp_restream =
    std::chrono::steady_clock::time_point::min();

void LogTcpTransport(ae::Client& client) {
  static bool logged_waiting = false;
  bool any_tcp = false;
  bool any_udp = false;
  int linked = 0;
  auto const now = std::chrono::steady_clock::now();
  bool const allow_restream =
      g_last_udp_restream == std::chrono::steady_clock::time_point::min() ||
      (now - g_last_udp_restream) > std::chrono::seconds(5);
  for (auto* csc : client.cloud_connection().servers()) {
    if (csc == nullptr) {
      continue;
    }
    auto* cc = csc->client_connection();
    if (cc == nullptr) {
      continue;
    }
    auto const info = cc->stream_info();
    if (info.link_state != ae::LinkState::kLinked) {
      continue;
    }
    ++linked;
    auto ch = cc->server_connection().current_channel();
    if (!ch) {
      continue;
    }
    if (auto ep = ch->endpoint()) {
      if (ep->protocol == ae::Protocol::kTcp) {
        any_tcp = true;
        std::cout << "RX_TCP_LINK_UP endpoint=" << ae::Format("{}", *ep)
                  << " server=" << csc->server_id() << "\n";
      } else if (ep->protocol == ae::Protocol::kUdp) {
        any_udp = true;
        std::cout << "RX_TRANSPORT=UDP endpoint=" << ae::Format("{}", *ep)
                  << " server=" << csc->server_id() << "\n";
        // Keep forcing TCP if cloud reselects UDP after the first Restream.
        if (allow_restream) {
          g_last_udp_restream = now;
          std::cout << "RX_TCP_RESELECT server=" << csc->server_id() << "\n";
          csc->Restream();
        }
      } else {
        std::cout << "RX_TRANSPORT_NOT_TCP protocol="
                  << static_cast<int>(ep->protocol) << " endpoint="
                  << ae::Format("{}", *ep) << "\n";
      }
    }
  }
  if (any_tcp && !any_udp) {
    g_tcp_links_up = 1;
    g_rx_tcp_verified = true;
    std::cout << "RX_TRANSPORT=TCP\n";
    logged_waiting = false;
  } else if (any_udp) {
    g_tcp_links_up = 0;
    g_rx_tcp_verified = false;
  } else if (linked == 0 && client.cloud_connection().count_connections() > 0) {
    if (!logged_waiting) {
      std::cout << "RX_TCP_LINK_DOWN waiting_for_tcp\n";
      logged_waiting = true;
    }
    g_tcp_links_up = 0;
  }
  std::cout.flush();
}

void EmitRxHealth() {
  std::cout << "RX_HEALTH full=" << g_full_recv << " hot=" << g_hot_recv
            << " dup=" << g_dup_records << " tcp_up=" << g_tcp_links_up
            << "\n";
  std::cout.flush();
}

int DsBlockCount() {
#if defined(_WIN32)
  if (char const* env = std::getenv("AE_DS_BLOCKS")) {
    int const v = std::atoi(env);
    if (v >= 1 && v <= 8) {
      return v;
    }
  }
#endif
  return 5;
}

int DsHotPerBlock() {
#if defined(_WIN32)
  if (char const* env = std::getenv("AE_DS_HOT_PER")) {
    int const v = std::atoi(env);
    if (v >= 1 && v <= 100) {
      return v;
    }
  }
#endif
  return 50;
}

std::filesystem::path TsvPath() {
#if defined(_WIN32)
  if (char const* env = std::getenv("AE_DS_TSV")) {
    return std::filesystem::path{env};
  }
#endif
  return std::filesystem::path{"prepared_mac_retry_diag.tsv"};
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
         "last_disconnect_reason\treconnect_count\tap_primary\t"
         "variant\tshort_retry\tlong_retry\tretry_called\tretry_set_rc\t"
         "retry_cfg_us\tencode_us\tactual_channel\twifi_init_us\t"
         "heap_before\theap_after\n";
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
      << static_cast<int>(m.ap_primary) << '\t'
      << static_cast<int>(m.variant) << '\t'
      << static_cast<int>(m.short_retry) << '\t'
      << static_cast<int>(m.long_retry) << '\t'
      << static_cast<int>(m.retry_called) << '\t'
      << static_cast<int>(m.retry_set_rc) << '\t' << m.retry_cfg_us << '\t'
      << m.encode_us << '\t' << static_cast<int>(m.actual_channel) << '\t'
      << m.wifi_init_us << '\t' << m.heap_before << '\t' << m.heap_after
      << '\n';
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

void OnBootWifiOpt(temp_sensor::bench::BootWifiOptPayload const& p) {
  auto const type = static_cast<temp_sensor::bench::BootWifiOptMsgType>(p.type);
  static int hot_by_var[16] = {};
  if (type == temp_sensor::bench::BootWifiOptMsgType::kFull) {
    ++g_full_recv;
    std::cout << "BWO_FULL seq=" << p.sequence_global
              << " variant=" << static_cast<unsigned>(p.variant_id)
              << " name="
              << temp_sensor::bench::BootWifiOptVariantName(p.variant_id)
              << " prev_v=" << static_cast<unsigned>(p.prev_variant_id)
              << " prev_sends=" << static_cast<unsigned>(p.prev_hot_send_count)
              << "\n";
  } else if (type == temp_sensor::bench::BootWifiOptMsgType::kHot) {
    ++g_hot_recv;
    auto vid = p.pending_kind == 2 ? p.pending_variant : p.variant_id;
    if (vid < 16) {
      ++hot_by_var[vid];
    }
    std::cout << "BWO V" << static_cast<unsigned>(vid) << " "
              << (vid < 16 ? hot_by_var[vid] : 0)
              << (vid == 0xff ? "/100" : "/30")
              << " wake_ov=" << (p.sleep_to_app_overhead_us / 1000.0) << "ms"
              << " init=" << (p.wifi_init_us / 1000.0) << "ms"
              << " conn=" << (p.connect_us / 1000.0) << "ms"
              << " txdone=" << (p.tx_done_wait_us / 1000.0) << "ms"
              << " user=" << (p.pending_user_cycle_us / 1000.0) << "ms"
              << " rssi=" << static_cast<int>(p.rssi) << "\n";
  } else if (type == temp_sensor::bench::BootWifiOptMsgType::kFinal) {
    ++g_final_recv;
    std::cout << "BWO_FINAL seq=" << p.sequence_global << "\n";
  }

  Meas m{};
  m.record_id = p.record_id;
  m.kind = p.pending_kind;
  m.outer = p.pending_variant;
  m.hot = p.pending_hot_index;
  m.user_us = p.pending_user_cycle_us;
  m.wifi_us = p.pending_wifi_cycle_us;
  m.connect_us = p.connect_us;
  m.txdone_us = p.tx_done_wait_us;
  m.teardown_us = p.teardown_us;
  m.sleep_elapsed_us = p.sleep_elapsed_to_app_us;
  m.sleep_overhead_us = p.sleep_to_app_overhead_us;
  m.app_entry_us = static_cast<std::uint32_t>(
      p.app_entry_esp_timer_us < 0
          ? 0
          : (p.app_entry_esp_timer_us > 0xffffffffll
                 ? 0xffffffffu
                 : static_cast<std::uint32_t>(p.app_entry_esp_timer_us)));
  m.cb_seen = (p.flags & 2) ? 1 : 0;
  m.cb_timeout = p.cb_timeout;
  m.brownout = (p.flags & 1) ? 1 : 0;
  m.auth = p.authmode;
  m.tx_cb_total = p.tx_cb_total;
  m.tx_cb_success = p.tx_cb_success;
  m.tx_cb_failed = p.tx_cb_failed;
  m.first_status = p.first_status;
  m.rssi = p.rssi;
  m.disconnect_count = p.disconnect_count;
  m.reconnect_count = p.reconnect_count;
  m.actual_channel = p.actual_channel;
  m.ap_primary = p.actual_channel;
  m.seq = p.sequence_global;
  m.variant = p.pending_kind == 2 ? p.pending_variant : p.variant_id;
  m.encode_us = p.encode_send_us;
  m.wifi_init_us = p.wifi_init_us;
  m.heap_before = p.heap_before_wifi;
  m.heap_after = p.heap_after_wifi;
  NoteRecord(m);

  if (type == temp_sensor::bench::BootWifiOptMsgType::kFinal) {
    PrintFinalStats("boot_wifi_opt");
  }
  std::cout.flush();
}

void OnMacRetry(temp_sensor::bench::MacRetryPayload const& p) {
  auto const type = static_cast<temp_sensor::bench::MacRetryMsgType>(p.type);
  static int hot_by_var[8] = {};
  if (type == temp_sensor::bench::MacRetryMsgType::kFull) {
    ++g_full_recv;
    std::cout << "MAC_FULL seq=" << p.sequence_global
              << " variant=" << static_cast<unsigned>(p.variant_id)
              << " name=" << temp_sensor::bench::MacRetryVariantName(p.variant_id)
              << " prev_v=" << static_cast<unsigned>(p.prev_variant_id)
              << " prev_sends=" << static_cast<unsigned>(p.prev_hot_send_count)
              << " prev_tx_ok=" << static_cast<unsigned>(p.prev_tx_success_count)
              << " prev_tx_fail=" << static_cast<unsigned>(p.prev_tx_fail_count)
              << "\n";
  } else if (type == temp_sensor::bench::MacRetryMsgType::kHot) {
    ++g_hot_recv;
    auto vid = p.pending_kind == 2 ? p.pending_variant : p.variant_id;
    if (vid < 8) {
      ++hot_by_var[vid];
    }
    char const* tx = "NA";
    if (p.first_status == 1) {
      tx = "OK";
    } else if (p.first_status == 0) {
      tx = "FAIL";
    }
    char const* cb = "none";
    if (p.cb_timeout) {
      cb = "timeout";
    } else if (p.tx_cb_success) {
      cb = "success";
    } else if (p.tx_cb_failed) {
      cb = "fail";
    }
    std::cout << "RETRY V" << static_cast<unsigned>(vid) << " "
              << (vid < 8 ? hot_by_var[vid] : 0) << "/50"
              << " s/l=" << static_cast<unsigned>(p.short_retry) << "/"
              << static_cast<unsigned>(p.long_retry)
              << " tx=" << tx << " cb=" << cb
              << " txdone=" << (p.tx_done_wait_us / 1000.0) << "ms"
              << " wifi=" << (p.pending_wifi_cycle_us / 1000.0) << "ms"
              << " rssi=" << static_cast<int>(p.rssi)
              << " rc=" << p.retry_set_rc
              << " recv=yes\n";
  } else if (type == temp_sensor::bench::MacRetryMsgType::kFinal) {
    ++g_final_recv;
    std::cout << "MAC_FINAL seq=" << p.sequence_global << "\n";
  }

  Meas m{};
  m.record_id = p.record_id;
  m.kind = p.pending_kind;
  m.outer = p.pending_variant;
  m.hot = p.pending_hot_index;
  m.user_us = p.pending_user_cycle_us;
  m.wifi_us = p.pending_wifi_cycle_us;
  m.connect_us = p.connect_us;
  m.txdone_us = p.tx_done_wait_us;
  m.teardown_us = p.teardown_us;
  m.encode_us = p.encode_send_us;
  m.cb_seen = (p.flags & 2) ? 1 : 0;
  m.cb_timeout = p.cb_timeout;
  m.brownout = (p.flags & 1) ? 1 : 0;
  m.auth = p.authmode;
  m.tx_cb_total = p.tx_cb_total;
  m.tx_cb_success = p.tx_cb_success;
  m.tx_cb_failed = p.tx_cb_failed;
  m.first_status = p.first_status;
  m.first_cb_delta_us = p.first_cb_delta_us;
  m.first_success_delta_us = p.first_success_delta_us;
  m.first_failed_delta_us = p.first_failed_delta_us;
  m.last_cb_delta_us = p.last_cb_delta_us;
  m.rssi = p.rssi;
  m.disconnect_count = p.disconnect_count;
  m.reconnect_count = p.reconnect_count;
  m.actual_channel = p.actual_channel;
  m.ap_primary = p.actual_channel;
  m.seq = p.sequence_global;
  m.variant = p.pending_kind == 2 ? p.pending_variant : p.variant_id;
  m.short_retry = p.short_retry;
  m.long_retry = p.long_retry;
  m.retry_called = p.retry_function_called;
  m.retry_set_rc = p.retry_set_rc;
  m.retry_cfg_us = p.retry_cfg_us;
  NoteRecord(m);

  if (type == temp_sensor::bench::MacRetryMsgType::kFinal) {
    PrintFinalStats("mac_retry");
  }
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
  static int hot_recv_by_outer[8] = {};
  static int hot_cb_by_outer[8] = {};
  static int hot_to_by_outer[8] = {};
  static std::uint32_t last_full_user[8] = {};
  static int printed_block = 0;

  char const* bench_tag = "deepsleep_5x50";
#if defined(_WIN32)
  if (char const* env = std::getenv("AE_DS_BENCH_TAG")) {
    if (env[0] != '\0') {
      bench_tag = env;
    }
  }
#endif

  if (type == temp_sensor::bench::DsMsgType::kFull) {
    ++g_full_recv;
    std::cout << "DS_FULL outer=" << static_cast<unsigned>(p.outer_cycle)
              << " seq=" << p.sequence_global << "\n";
  } else if (type == temp_sensor::bench::DsMsgType::kHot) {
    ++g_hot_recv;
    auto const outer = p.pending_kind == 2 ? p.pending_outer : p.outer_cycle;
    if (outer >= 1 && outer <= 5) {
      ++hot_recv_by_outer[outer];
      if (p.flags & static_cast<std::uint8_t>(
                        temp_sensor::bench::DsFlags::kCallbackSeen)) {
        ++hot_cb_by_outer[outer];
      }
      if (p.flags & static_cast<std::uint8_t>(
                        temp_sensor::bench::DsFlags::kCallbackTimeout)) {
        ++hot_to_by_outer[outer];
      }
    }
    std::cout << "DS_HOT B" << static_cast<unsigned>(outer) << " "
              << (outer <= 5 ? hot_recv_by_outer[outer] : 0) << "/50"
              << " user=" << (p.pending_user_cycle_us / 1000.0) << "ms"
              << " wifi=" << (p.pending_wifi_cycle_us / 1000.0) << "ms"
              << " wake_ov=" << (p.sleep_to_app_overhead_us / 1000.0) << "ms"
              << "\n";
  } else if (type == temp_sensor::bench::DsMsgType::kFinal) {
    ++g_final_recv;
    std::cout << "DS_FINAL seq=" << p.sequence_global << "\n";
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
  m.disconnect_count = p.disconnect_count;
  m.last_disconnect_reason = p.last_disconnect_reason;
  m.reconnect_count = p.reconnect_count;
  m.rssi = p.rssi;
  m.actual_channel = p.actual_channel;
  if (p.failed_assoc_wakes > g_failed_assoc_wakes) {
    g_failed_assoc_wakes = p.failed_assoc_wakes;
  }
  NoteRecord(m);

  int const block_count = DsBlockCount();
  int const hot_per = DsHotPerBlock();
  if (p.pending_kind == 1 && p.pending_outer >= 1 &&
      p.pending_outer <= static_cast<std::uint8_t>(block_count)) {
    last_full_user[p.pending_outer] = p.pending_user_cycle_us;
  }

  // Block summary when a FULL of next outer arrives (prev outer complete) or FINAL.
  auto print_block = [&](int b) {
    if (b < 1 || b > block_count || b <= printed_block) {
      return;
    }
    printed_block = b;
    std::vector<std::uint32_t> users;
    std::vector<std::uint32_t> wifis;
    std::vector<std::uint32_t> wakes;
    for (auto const& mm : g_meas) {
      if (mm.kind == 2 && mm.outer == b) {
        users.push_back(mm.user_us);
        wifis.push_back(mm.wifi_us);
        wakes.push_back(mm.sleep_overhead_us);
      }
    }
    std::cout << "[BLOCK " << b << "/" << block_count << "]\n"
              << "FULL=1/1\n"
              << "HOT_SENDTO=" << hot_per << "/" << hot_per << "\n"
              << "HOT_RECEIVED=" << hot_recv_by_outer[b] << "/" << hot_per
              << "\n"
              << "callback=" << hot_cb_by_outer[b] << "/" << hot_per << "\n"
              << "timeouts=" << hot_to_by_outer[b] << "\n"
              << "hot_user_med=" << PercentileUs(users, 50) << "\n"
              << "hot_wifi_med=" << PercentileUs(wifis, 50) << "\n"
              << "wake_med=" << PercentileUs(wakes, 50) << "\n"
              << "full_user=" << last_full_user[b] << "\n"
              << "remaining=" << (block_count - b) << "\n";
    std::cout.flush();
  };

  if (type == temp_sensor::bench::DsMsgType::kFull && p.outer_cycle >= 2) {
    print_block(static_cast<int>(p.outer_cycle) - 1);
  }
  if (type == temp_sensor::bench::DsMsgType::kFinal) {
    print_block(block_count);
    PrintFinalStats(bench_tag);
  }
}

void OnNosleep(temp_sensor::bench::NosleepPayload const& p) {
  auto const type = static_cast<temp_sensor::bench::NosleepMsgType>(p.type);
  static int hot_by_outer[8] = {};
  static int hot_cb[8] = {};
  static int hot_to[8] = {};
  static int ch_fb = 0;
  static int dhcp_fb = 0;
  static int arp_fb = 0;
  static int printed_outer = 0;
  static char last_ssid[33] = {};
  static std::uint8_t last_bssid[6] = {};
  static std::uint8_t last_sta[6] = {};

  if (p.ssid[0] != '\0') {
    std::memcpy(last_ssid, p.ssid, sizeof(last_ssid));
  }
  if (p.bssid[0] | p.bssid[1] | p.bssid[2]) {
    std::memcpy(last_bssid, p.bssid, 6);
  }
  if (p.sta_mac[0] | p.sta_mac[1] | p.sta_mac[2]) {
    std::memcpy(last_sta, p.sta_mac, 6);
  }

  auto flag = [&](temp_sensor::bench::NosleepFlags f) {
    return (p.flags & static_cast<std::uint8_t>(f)) != 0;
  };
  if (flag(temp_sensor::bench::NosleepFlags::kChannelFallback)) {
    ++ch_fb;
  }
  if (flag(temp_sensor::bench::NosleepFlags::kDhcpFallback)) {
    ++dhcp_fb;
  }
  if (flag(temp_sensor::bench::NosleepFlags::kArpFallback)) {
    ++arp_fb;
  }

  if (type == temp_sensor::bench::NosleepMsgType::kFull) {
    ++g_full_recv;
    std::cout << "NS_FULL outer=" << static_cast<unsigned>(p.outer_cycle)
              << " seq=" << p.sequence_global << " ssid=" << p.ssid
              << " ch=" << static_cast<unsigned>(p.channel)
              << " rssi=" << static_cast<int>(p.rssi) << "\n";
    std::cout << "  bssid=" << std::hex
              << static_cast<int>(p.bssid[0]) << ":"
              << static_cast<int>(p.bssid[1]) << ":"
              << static_cast<int>(p.bssid[2]) << ":"
              << static_cast<int>(p.bssid[3]) << ":"
              << static_cast<int>(p.bssid[4]) << ":"
              << static_cast<int>(p.bssid[5]) << std::dec
              << " sta=";
    for (int i = 0; i < 6; ++i) {
      if (i) {
        std::cout << ":";
      }
      std::cout << std::hex << static_cast<int>(p.sta_mac[i]);
    }
    std::cout << std::dec << "\n";
  } else if (type == temp_sensor::bench::NosleepMsgType::kHot) {
    ++g_hot_recv;
    auto const outer = p.outer_cycle;
    if (outer >= 1 && outer <= 5) {
      ++hot_by_outer[outer];
      if (p.cb_seen) {
        ++hot_cb[outer];
      }
      if (p.cb_timeout) {
        ++hot_to[outer];
      }
    }
    std::cout << "NS_HOT O" << static_cast<unsigned>(outer) << " "
              << "meas_hot=" << static_cast<unsigned>(p.hot_index)
              << " connect_ms=" << (p.connect_us / 1000.0)
              << " total_ms=" << (p.hot_total_us / 1000.0)
              << " ch_fb=" << (flag(temp_sensor::bench::NosleepFlags::kChannelFallback) ? 1 : 0)
              << " dhcp_fb=" << (flag(temp_sensor::bench::NosleepFlags::kDhcpFallback) ? 1 : 0)
              << " arp_fb=" << (flag(temp_sensor::bench::NosleepFlags::kArpFallback) ? 1 : 0)
              << " cb_to=" << static_cast<unsigned>(p.cb_timeout)
              << "\n";
  } else if (type == temp_sensor::bench::NosleepMsgType::kHotFail) {
    std::cout << "NS_HOT_FAIL O" << static_cast<unsigned>(p.outer_cycle)
              << " hot=" << static_cast<unsigned>(p.hot_index)
              << " stage=" << static_cast<unsigned>(p.fail_stage)
              << " disc=" << static_cast<unsigned>(p.disc_reason)
              << " connect_ms=" << (p.connect_us / 1000.0) << "\n";
  } else if (type == temp_sensor::bench::NosleepMsgType::kFinal) {
    ++g_final_recv;
    std::cout << "NS_FINAL seq=" << p.sequence_global << "\n";
  }

  Meas m{};
  m.record_id = p.record_id;
  m.kind = p.type;
  m.outer = p.outer_cycle;
  m.hot = p.hot_index;
  m.user_us = p.hot_total_us;
  m.wifi_us = p.hot_total_us;
  m.connect_us = p.connect_us;
  m.txdone_us = p.tx_done_us;
  m.teardown_us = p.teardown_us;
  m.cb_seen = p.cb_seen;
  m.cb_timeout = p.cb_timeout;
  m.auth = p.hot_auth ? p.hot_auth : p.authmode;
  m.seq = p.sequence_global;
  m.rssi = p.hot_rssi ? p.hot_rssi : p.rssi;
  m.actual_channel = p.hot_channel ? p.hot_channel : p.channel;
  m.disconnect_count = p.disc_count;
  m.last_disconnect_reason = p.disc_reason;
  m.reconnect_count = p.reconnect_count;
  NoteRecord(m);

  auto print_outer = [&](int o) {
    if (o < 1 || o > 5 || o <= printed_outer) {
      return;
    }
    printed_outer = o;
    std::cout << "[OUTER " << o << "/5]\n"
              << "FULL received=1\n"
              << "HOT sendto=5/5\n"
              << "HOT received=" << hot_by_outer[o] << "/5\n"
              << "connect_med=" << 0 << "\n"
              << "channel_fallback=" << ch_fb << "\n"
              << "dhcp_fallback=" << dhcp_fb << "\n"
              << "arp_fallback=" << arp_fb << "\n"
              << "callback_timeout=" << hot_to[o] << "\n"
              << "remaining=" << (5 - o) << "\n";
    std::cout.flush();
  };

  if (type == temp_sensor::bench::NosleepMsgType::kFull &&
      p.outer_cycle >= 2) {
    print_outer(static_cast<int>(p.outer_cycle) - 1);
  }
  if (type == temp_sensor::bench::NosleepMsgType::kFinal) {
    print_outer(5);
    char const* tag = "ap_aethernetio_nosleep_5x5";
#if defined(_WIN32)
    if (char const* env = std::getenv("AE_DS_BENCH_TAG")) {
      if (env[0] != '\0') {
        tag = env;
      }
    }
#endif
    PrintFinalStats(tag);
  }
  std::cout.flush();
}

void OnMessage(ae::Uid, ae::DataBuffer const& data) {
  std::lock_guard lock{g_mu};

  temp_sensor::bench::ReliabilityPayload rp{};
  if (temp_sensor::bench::DecodeReliability(data, rp)) {
    static std::uint32_t run_id = 1;
    static std::uint32_t current_seq = 0;
    static std::uint64_t received = 0;
    static std::uint64_t lost = 0;
    static std::uint64_t duplicates = 0;
    static bool have_seq = false;
    static std::ofstream tsv;
    static bool tsv_ok = false;

    if (!tsv_ok) {
      auto const session_root = ResolveSessionRoot();
      auto const path = session_root / "reliability_rx.tsv";
      tsv.open(path, std::ios::app);
      if (tsv) {
        tsv << "wall_ms\trun_id\tpayload_run\tseq\treceived\tlost\tdup\n";
        tsv_ok = true;
      }
    }

    auto print_line = [&]() {
      double loss_pct = 0.0;
      auto const denom = received + lost;
      if (denom > 0) {
        loss_pct = (100.0 * static_cast<double>(lost)) /
                   static_cast<double>(denom);
      }
      std::cout << "RUN=" << run_id << " seq=" << rp.seq << " recv=" << received
                << " lost=" << lost << " loss=" << std::fixed
                << std::setprecision(2) << loss_pct << "%"
                << " dup=" << duplicates << "\n";
      std::cout.flush();
      if (tsv_ok) {
        auto const ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::system_clock::now().time_since_epoch())
                            .count();
        tsv << ms << '\t' << run_id << '\t' << rp.run_id << '\t' << rp.seq
            << '\t' << received << '\t' << lost << '\t' << duplicates << '\n';
        tsv.flush();
      }
      if (received > 0 && (received % 50) == 0) {
        std::cout << "=== SUMMARY === run=" << run_id
                  << " last_seq=" << current_seq << " received=" << received
                  << " lost=" << lost << " loss_percent=" << std::fixed
                  << std::setprecision(2) << loss_pct
                  << " duplicates=" << duplicates << "\n";
        std::cout.flush();
      }
    };

    if (!have_seq || rp.seq < current_seq) {
      if (have_seq) {
        ++run_id;
        std::cout << "=== NEW RUN DETECTED === previous_seq=" << current_seq
                  << " new_seq=" << rp.seq << " run=" << run_id << "\n";
        std::cout.flush();
      }
      have_seq = true;
      current_seq = rp.seq;
      received = 1;
      lost = 0;
      duplicates = 0;
      print_line();
      return;
    }
    if (rp.seq == current_seq) {
      ++duplicates;
      print_line();
      return;
    }
    lost += static_cast<std::uint64_t>(rp.seq - current_seq - 1);
    ++received;
    current_seq = rp.seq;
    print_line();
    return;
  }

  if (!data.empty() && data[0] >= 0x20 && data[0] < 0x7f) {
    std::cout.write(reinterpret_cast<char const*>(data.data()),
                    static_cast<std::streamsize>(data.size()));
    std::cout << "\n";
    std::cout.flush();
    return;
  }
  temp_sensor::bench::NosleepPayload ns{};
  if (temp_sensor::bench::DecodeNosleep(data, ns)) {
    OnNosleep(ns);
    return;
  }
  temp_sensor::bench::BootWifiOptPayload bwo{};
  if (temp_sensor::bench::DecodeBootWifiOpt(data, bwo)) {
    OnBootWifiOpt(bwo);
    return;
  }
  temp_sensor::bench::MacRetryPayload mr{};
  if (temp_sensor::bench::DecodeMacRetry(data, mr)) {
    OnMacRetry(mr);
    return;
  }
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
        std::cout << "========================================\n"
                  << ae::Format("RECEIVER_UID={}\n", client->uid())
                  << "========================================\n";
        std::cout.flush();
        client->connectivity_policy()->ResetRxTimings();
        client->connectivity_policy()
            ->ConfigureRxTimings(ae::RequestPolicy::All{})
            .ForAllPriorities(ae::RxTimingConf::Every(1s).WithWindow(1s));
        LogTcpTransport(*client);
        client->cloud_connection().servers_update_event().Subscribe(
            [client]() { LogTcpTransport(*client); });
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
      LogTcpTransport(*client);
      EmitRxHealth();
    }
    aether_app->WaitUntil(t);
  }
  return aether_app->ExitCode();
}
