/*

 * AE_EXP_SIMPLE_FULL_HOT_101: one scenario only.

 * Message 1 — FULL Æther + FULL_WIFI=vanilla (scan, DHCP, WIFI_PS_NONE).

 * Messages 2…101 — prepared HOT + HOT_WIFI=vanilla (scan, DHCP, dynamic ARP).

 * Payload body: ASCII message number only. Deep sleep 2 s between cycles.

 */



#include <cstdio>

#include <cstring>

#include <memory>

#include <string>



#include "aether/adapters/wifi_adapter.h"

#include "aether/all.h"

#include "aether/ae_exp_wifi.h"

#include "aether/config.h"

#include "aether/env.h"

#include "prepared_send/prepared_send.h"
#include "udp_cached_lwip_send.h"



#include <esp_event.h>

#include <esp_sleep.h>

#include <esp_system.h>

#include <esp_task_wdt.h>

#include <esp_wifi.h>

#include <freertos/FreeRTOS.h>

#include <freertos/task.h>

#include <nvs_flash.h>

#include <esp_timer.h>

#include <esp_err.h>
#include <esp_wifi.h>

#include "bench_payload.h"

static bool g_ae_exp_got_ip_flag = false;

namespace temp_sensor {

namespace {



using temp_sensor::prepared_send::PreparedWifiRtcCache;



static constexpr auto kParentUid =

    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");

static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);



#ifndef AE_SIMPLE_FULL_HOT_COUNT
#define AE_SIMPLE_FULL_HOT_COUNT 100
#endif

static constexpr int kHotCount = AE_SIMPLE_FULL_HOT_COUNT;

static constexpr int kLastMessage = 1 + kHotCount;

static constexpr std::uint64_t kSleepUs = 2'000'000;

static constexpr std::uint16_t kHalted = 0xffffu;

static constexpr std::uint16_t kHotPostSendHoldMs = 1000;

struct HotAttemptRecord {
  // bit0 got_ip bit1 encode_ok bit2 nonce bit3 sendto_ok bit4 started
  std::uint8_t flags{0};
  std::uint8_t encode_called{0};
  std::uint8_t sendto_called{0};
  std::uint8_t err{0};  // 0=ok 1=wifi 2=encode 3=send 4=nonce 5=block 6=other
};

static constexpr std::uint32_t kHotRtcMagic = 0x48545231u;  // HTR1

struct HotRtcTable {
  std::uint32_t magic{kHotRtcMagic};
  std::uint16_t version{2};
  std::uint32_t run_id{0};
  std::uint16_t planned{static_cast<std::uint16_t>(kHotCount)};
  std::uint16_t next_message{1};
  HotAttemptRecord row[kHotCount]{};
};

RTC_DATA_ATTR static HotRtcTable g_hot_rtc{};
RTC_DATA_ATTR static std::uint32_t g_hot_run_serial{0};
static char g_dump_finish_reason[64] = "unknown";



RTC_DATA_ATTR static std::uint16_t g_next_message = 1;

RTC_DATA_ATTR static PreparedWifiRtcCache g_wifi_cache{};



static std::shared_ptr<ae::AetherApp> g_app;

static ae::Client::ptr g_client;

static std::unique_ptr<ae::P2pStream> g_stream;

static ae::Subscription g_select_sub;

static ae::Subscription g_stream_sub;

static ae::Subscription g_write_sub;

static bool g_write_started = false;

static bool g_write_ok = false;

static bool g_scenario_failed = false;

static char g_fail_stage[48] = {};

static char g_fail_error[64] = {};

static bool g_stream_writable_logged = false;
static ae::Client::ptr g_pending_client;
static bool g_select_ready_pending = false;

struct BootRtcState {
  static constexpr std::uint32_t kMagic = 0x53464831u;
  std::uint32_t magic{kMagic};
  std::uint8_t bootstrap_complete{0};
  std::uint8_t pad[3]{};
  std::uint8_t sender_uid[16]{};
  std::uint32_t crc{0};
};

RTC_DATA_ATTR static BootRtcState g_boot_rtc{};

static bool g_bootstrap_active = false;
static bool g_bootstrap_select_done = false;
static bool g_bootstrap_select_ok = false;
static int g_bootstrap_select_err = 0;
static bool g_bootstrap_write_armed = false;
static bool g_bootstrap_write_terminal = false;
static ae::WriteAction::Status g_bootstrap_write_status =
    ae::WriteAction::Status::kStop;
static ae::Subscription g_bootstrap_select_sub;
static ae::Subscription g_bootstrap_write_sub;

static std::int64_t g_bootstrap_deadline_us = 0;

static ae::WiFiInit VanillaWifi() {

  return ae::WiFiInit{

      std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},

      std::nullopt,

  };

}



static void ApplyVanillaWifiInitToRegistry() {

  if (!g_app || !g_app->aether() || !g_app->aether()->adapter_registry) {

    return;

  }

  auto const init = VanillaWifi();

  for (auto const& adapter : g_app->aether()->adapter_registry->adapters()) {

    ae::WifiAdapter::ptr wa{adapter};

    if (!wa.is_valid()) {

      continue;

    }

    if (auto loaded = wa.Load()) {

      loaded->wifi_init_ = init;

    }

  }

  std::printf("FULL_WIFI_PSP_PRESENT=0\n");

  std::fflush(stdout);

}



static ae::DataBuffer Payload(int n) {

  std::string s = std::to_string(n);

  return ae::DataBuffer{s.begin(), s.end()};

}



static temp_sensor::prepared_send::FastPathConfig VanillaHotWifi() {

  temp_sensor::prepared_send::FastPathConfig c{};

  c.use_bssid = false;

  c.use_channel = false;

  c.use_fast_scan = false;

  c.use_static_ip = false;

  c.use_static_arp = false;

  c.ampdu_tx_off = false;

  c.ampdu_rx_off = false;

  c.amsdu_tx_off = false;

  c.force_ht20 = false;

  c.fixed_1m = false;

  c.set_mac_retry_limit = false;

  c.ps_max_modem = false;

  c.use_default_tx_power = true;

  c.auth = temp_sensor::prepared_send::FastAuthMode::kWpa2;

  c.retry_max = 10;

  c.pre_delay_ms = 0;

  c.post_delay_ms = kHotPostSendHoldMs;

  c.post_mode = temp_sensor::prepared_send::FastPostMode::kFixedDelay;

  c.arp_wait_on_miss = false;

  return c;

}



static int HotRowIndex(int message_num) {

  if (message_num < 2 || message_num > kLastMessage) {

    return -1;

  }

  return message_num - 2;

}



static void ResetHotRtcTable() {

  ++g_hot_run_serial;

  g_hot_rtc = HotRtcTable{};

  g_hot_rtc.run_id = g_hot_run_serial;

  g_hot_rtc.next_message = g_next_message;

}



static void SetDumpFinishReason(char const* reason) {

  if (reason == nullptr || reason[0] == '\0') {

    return;

  }

  std::snprintf(g_dump_finish_reason, sizeof g_dump_finish_reason, "%s", reason);

}



static int CountHotAttemptsStarted() {

  int n = 0;

  for (int i = 0; i < kHotCount; ++i) {

    if ((g_hot_rtc.row[i].flags & 0x10) != 0) {

      ++n;

    }

  }

  return n;

}



static void MarkHotAttemptStarted(int num) {

  int const idx = HotRowIndex(num);

  if (idx < 0) {

    return;

  }

  HotAttemptRecord rec{};

  rec.flags = 0x10;

  g_hot_rtc.row[idx] = rec;

}



static std::uint8_t HotErrFromStatus(

    temp_sensor::prepared_send::HotSendStatus st) {

  using temp_sensor::prepared_send::HotSendStatus;

  switch (st) {

    case HotSendStatus::kSent:

    case HotSendStatus::kSentTxUnconfirmed:

      return 0;

    case HotSendStatus::kWifiFailed:

      return 1;

    case HotSendStatus::kEncodeFailed:

      return 2;

    case HotSendStatus::kSendFailed:

      return 3;

    case HotSendStatus::kNonceExhausted:

      return 4;

    case HotSendStatus::kNoPreparedBlock:

      return 5;

    default:

      return 6;

  }

}



static void RecordHotAttempt(

    int num, temp_sensor::prepared_send::FastSendResult const& result) {

  int const idx = HotRowIndex(num);

  if (idx < 0) {

    return;

  }

  HotAttemptRecord rec{};

  rec.flags = 0x10;

  if (result.got_ip_seen) {

    rec.flags |= 0x01;

  }

  rec.encode_called =

      (result.encode_us > 0 || result.fail_stage >= 2 ||

       (result.status_flags &

        static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk)) != 0)

          ? 1

          : 0;

  if ((result.status_flags &

       static_cast<std::uint8_t>(bench::BisectStatusBits::kEncodeOk)) != 0) {

    rec.flags |= 0x02;

  }

  if (temp_sensor::prepared_send::HotSendConsumedNonce(result.status)) {

    rec.flags |= 0x04;

  }

  rec.sendto_called =

      (result.sendto_begin_us > 0 || result.sendto_ok) ? 1 : 0;

  if (result.sendto_ok) {

    rec.flags |= 0x08;

  }

  rec.err = HotErrFromStatus(result.status);

  g_hot_rtc.row[idx] = rec;

  g_hot_rtc.next_message = static_cast<std::uint16_t>(num + 1);

}



static void DumpHotRtcTable() {

  bool const table_ok = g_hot_rtc.magic == kHotRtcMagic;

  int const started = CountHotAttemptsStarted();

  char const* table_tag = table_ok ? "OK" : "UNAVAILABLE";

  std::printf(

      "HOT_RTC_TABLE_BEGIN run_id=%u planned=%u started=%u next=%u "

      "finish=%s table=%s\n",

      static_cast<unsigned>(g_hot_rtc.run_id),

      static_cast<unsigned>(g_hot_rtc.planned),

      static_cast<unsigned>(started),

      static_cast<unsigned>(g_hot_rtc.next_message), g_dump_finish_reason,

      table_tag);

  if (table_ok) {

    for (int n = 2; n <= kLastMessage; ++n) {

      int const idx = HotRowIndex(n);

      HotAttemptRecord const& r = g_hot_rtc.row[idx];

      char const* state = "OK";

      bool const was_started = (r.flags & 0x10) != 0;

      if (!was_started && n >= static_cast<int>(g_hot_rtc.next_message)) {

        state = "NOT_RUN";

      } else if (was_started && r.err == 0 && (r.flags & 0x0f) == 0) {

        state = "IN_PROGRESS";

      } else if (!was_started && r.err == 0 && (r.flags & 0x0f) == 0) {

        state = "UNKNOWN";

      } else if (r.err != 0) {

        state = "FAIL";

      }

      std::printf(

          "HOT_RTC_ROW n=%d got_ip=%u encode_called=%u encode_ok=%u "

          "nonce=%u sendto_called=%u sendto=%u err=%u state=%s\n",

          n, static_cast<unsigned>((r.flags >> 0) & 1),

          static_cast<unsigned>(r.encode_called),

          static_cast<unsigned>((r.flags >> 1) & 1),

          static_cast<unsigned>((r.flags >> 2) & 1),

          static_cast<unsigned>(r.sendto_called),

          static_cast<unsigned>((r.flags >> 3) & 1),

          static_cast<unsigned>(r.err), state);

    }

  }

  std::printf("HOT_RTC_TABLE_END\n");

  std::fflush(stdout);

}



[[noreturn]] static void StayAwakeForRunnerDump(char const* finish_reason) {

  SetDumpFinishReason(finish_reason);

  for (;;) {

    DumpHotRtcTable();

    vTaskDelay(pdMS_TO_TICKS(2000));

    esp_task_wdt_reset();

  }

}



static char const* WriteStatusName(ae::WriteAction::Status st) {

  switch (st) {

    case ae::WriteAction::Status::kSuccess:

      return "Success";

    case ae::WriteAction::Status::kFail:

      return "Fail";

    case ae::WriteAction::Status::kStop:

      return "Stop";

  }

  return "Unknown";

}



static void MarkScenarioFail(char const* stage, char const* error) {

  if (g_scenario_failed) {

    return;

  }

  g_scenario_failed = true;

  std::snprintf(g_fail_stage, sizeof(g_fail_stage), "%s", stage);

  std::snprintf(g_fail_error, sizeof(g_fail_error), "%s", error ? error : "");

  g_next_message = kHalted;

  std::printf("SCENARIO_FAIL stage=%s error=%s\n", g_fail_stage, g_fail_error);

  std::fflush(stdout);

  std::snprintf(g_dump_finish_reason, sizeof g_dump_finish_reason,

                "SCENARIO_FAIL:%s", g_fail_stage);

}



static std::uint32_t BootRtcCrc32(void const* data, std::size_t len) {
  auto const* p = static_cast<std::uint8_t const*>(data);
  std::uint32_t crc = 0xffffffffu;
  for (std::size_t i = 0; i < len; ++i) {
    crc ^= p[i];
    for (int b = 0; b < 8; ++b) {
      std::uint32_t const mask = -(crc & 1u);
      crc = (crc >> 1) ^ (0xedb88320u & mask);
    }
  }
  return ~crc;
}



static bool ValidBootRtc(BootRtcState const& s) {
  if (s.magic != BootRtcState::kMagic) {
    return false;
  }
  BootRtcState tmp = s;
  tmp.crc = 0;
  return BootRtcCrc32(&tmp, sizeof(tmp)) == s.crc;
}



static void StoreBootRtc() {
  g_boot_rtc.crc = 0;
  g_boot_rtc.crc = BootRtcCrc32(&g_boot_rtc, sizeof(g_boot_rtc));
}



static void StoreSenderUid(ae::Uid const& uid) {
  std::memcpy(g_boot_rtc.sender_uid, uid.value.data(), ae::Uid::kSize);
  StoreBootRtc();
}



static bool SenderUidMatchesRtc(ae::Uid const& uid) {
  if (g_boot_rtc.bootstrap_complete == 0) {
    return true;
  }
  return std::memcmp(g_boot_rtc.sender_uid, uid.value.data(), ae::Uid::kSize) ==
         0;
}



[[noreturn]] static void DeepSleep2s() {

  std::printf("SLEEP_ENTER\n");

  std::fflush(stdout);

  esp_sleep_enable_timer_wakeup(kSleepUs);

  esp_deep_sleep_start();

}



[[noreturn]] static void ScenarioFailIdle() {

  StayAwakeForRunnerDump(g_dump_finish_reason);

}



static void ReleaseApp() {

  g_select_sub.Reset();

  g_stream_sub.Reset();

  g_write_sub.Reset();

  g_stream.reset();

  g_client = {};

  g_app.reset();

}



static void ConstructVanillaAether() {
  g_app = ae::AetherApp::Construct(
      ae::AetherAppContext{}
#if AE_DISTILLATION
          .AdaptersFactory([&](ae::AetherAppContext const& ctx) {
            auto ap = ae::AdapterRegistry::ptr{ctx.aether()->adapter_registry};
            if (!ap.is_valid()) {
              ap = ae::AdapterRegistry::ptr::Create(
                  ae::CreateWith{ctx.domain()}
                      .with_id(ae::GlobalId::kAdapterRegistry)
                      .with_flags(ae::ObjFlags::kUnloadedByDefault));
            }
            auto loaded_reg = ap.Load();
            bool have_wifi = false;
            for (auto const& adapter : loaded_reg->adapters()) {
              if (ae::WifiAdapter::ptr{adapter}.is_valid()) {
                have_wifi = true;
                break;
              }
            }
            if (!have_wifi) {
              loaded_reg->Add(ae::WifiAdapter::ptr::Create(
                  ae::CreateWith{ctx.domain()}, ctx.aether(), ctx.poller(),
                  ctx.dns_resolver(), VanillaWifi()));
            }
            return ap;
          })
#endif
  );
  ApplyVanillaWifiInitToRegistry();
}



static void FinishFullPath() {

  if (!g_write_ok) {

    MarkScenarioFail("full_write", "not_success");

    return;

  }

  bool cache_ok =

      temp_sensor::prepared_send::CapturePreparedWifiRtcCache(&g_wifi_cache);

  std::printf("WIFI_CACHE_RESULT=%s\n", cache_ok ? "ok" : "fail");

  std::fflush(stdout);

  if (!cache_ok) {

    MarkScenarioFail("wifi_cache", "capture_failed");

    return;

  }

  bool export_ok = temp_sensor::prepared_send::ExportPreparedSendBlock(

      g_client, kServiceUid, kHotCount);

  std::uint32_t remaining = temp_sensor::prepared_send::PreparedMessageLeft();

  std::printf("PREPARED_EXPORT_RESULT=%s remaining=%u\n",

              export_ok ? "ok" : "fail", static_cast<unsigned>(remaining));

  std::fflush(stdout);

  if (!export_ok) {

    MarkScenarioFail("prepared_export", "export_failed");

    return;

  }

  if (!temp_sensor::prepared_send::HasPreparedSendBlock() ||

      remaining != static_cast<std::uint32_t>(kHotCount)) {

    MarkScenarioFail("prepared_export", "remaining_mismatch");

    return;

  }

  g_app->aether().Save();

  ReleaseApp();

  temp_sensor::prepared_send::ReleaseFullAetherWifiForHotPath();

  g_next_message = 2;

  DeepSleep2s();

}



static void TryFullWrite() {

  if (!g_stream || g_write_started || g_scenario_failed) {

    return;

  }

  bool writable = g_stream->stream_info().is_writable;

  if (!g_stream_writable_logged) {

    std::printf("STREAM_STATE writable=%d\n", writable ? 1 : 0);

    std::fflush(stdout);

    g_stream_writable_logged = true;

  }

  if (!writable) {

    return;

  }

  g_write_started = true;

  std::printf("FULL_WRITE_BEGIN\n");

  std::fflush(stdout);

  auto& wa = g_stream->Write(Payload(1));

  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {

    std::printf("FULL_WRITE_STATUS=%s\n", WriteStatusName(st));

    std::fflush(stdout);

    if (st == ae::WriteAction::Status::kSuccess) {

      g_write_ok = true;

      return;

    }

    if (st == ae::WriteAction::Status::kFail ||

        st == ae::WriteAction::Status::kStop) {

      MarkScenarioFail("full_write", WriteStatusName(st));

    }

  });

}



static void BootstrapTryWrite() {

  if (!g_bootstrap_active || !g_stream || g_bootstrap_write_armed) {

    return;

  }

  if (!g_stream->stream_info().is_writable) {

    return;

  }

  temp_sensor::bench::ReliabilityPayload p{};

  p.type = static_cast<std::uint8_t>(
      temp_sensor::bench::ReliabilityMsgType::kFullReady);

  p.run_id = 0;

  p.seq = 0;

  auto payload = temp_sensor::bench::EncodeReliability<ae::DataBuffer>(p);

  g_bootstrap_write_armed = true;

  std::printf("BOOTSTRAP_WRITE_BEGIN\n");

  std::fflush(stdout);

  auto& wa = g_stream->Write(std::move(payload));

  g_bootstrap_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {

    g_bootstrap_write_status = st;

    if (st == ae::WriteAction::Status::kSuccess ||

        st == ae::WriteAction::Status::kFail ||

        st == ae::WriteAction::Status::kStop) {

      g_bootstrap_write_terminal = true;

      std::printf("BOOTSTRAP_WRITE_STATUS=%s\n", WriteStatusName(st));

      std::fflush(stdout);

    }

  });

}



static void OnClientReady(ae::Client::ptr client) {

  g_client = std::move(client);

  auto loaded = g_client.Load();

  if (!loaded) {

    MarkScenarioFail("select_client", "client_load_failed");

    return;

  }

  if (!SenderUidMatchesRtc(loaded->uid())) {

    MarkScenarioFail("select_client", "sender_uid_mismatch");

    return;

  }

  auto handle = loaded->message_stream_manager().CreatePort(kServiceUid);

  g_stream = std::make_unique<ae::P2pStream>(*g_app, loaded, kServiceUid,

                                               std::move(handle));

  g_stream_sub = g_stream->stream_update_event().Subscribe([]() {

    if (g_bootstrap_active) {

      BootstrapTryWrite();

      return;

    }

    TryFullWrite();

  });

  if (g_bootstrap_active) {

    BootstrapTryWrite();

  } else {

    TryFullWrite();

  }

}



static void StartBootstrapPhase() {

  g_bootstrap_active = true;

  g_bootstrap_select_done = false;

  g_bootstrap_select_ok = false;

  g_bootstrap_select_err = 0;

  g_bootstrap_write_armed = false;

  g_bootstrap_write_terminal = false;

  std::printf("BOOTSTRAP_BEGIN client_id=%s FS_INIT=0\n", kBenchClientId);

  std::fflush(stdout);

  ConstructVanillaAether();

  g_bootstrap_deadline_us = esp_timer_get_time() + 180LL * 1000000;

  g_ae_exp_got_ip_flag = false;

  g_bootstrap_select_sub = g_app->aether()

                                 ->SelectClient(kParentUid, kBenchClientId)

                                 .result_event()

                                 .Subscribe([](ae::Result<ae::Client::ptr, int> res) {

                                   g_bootstrap_select_done = true;

                                   if (!res) {

                                     g_bootstrap_select_err = res.error();

                                     std::printf(

                                         "BOOTSTRAP_SELECT ok=0 error=%d "

                                         "got_ip=%u\n",

                                         g_bootstrap_select_err,

                                         static_cast<unsigned>(
                                             g_ae_exp_got_ip_flag ? 1 : 0));

                                     std::fflush(stdout);

                                     MarkScenarioFail("bootstrap_select",

                                                      "select_error");

                                     return;

                                   }

                                   auto client = std::move(res).value();

                                   if (!client.Load()) {

                                     g_bootstrap_select_ok = false;

                                     g_bootstrap_select_err = 3;

                                     std::printf("BOOTSTRAP_SELECT ok=0 error=3\n");

                                     std::fflush(stdout);

                                     MarkScenarioFail("bootstrap_select",

                                                      "client_load_failed");

                                     return;

                                   }

                                   g_bootstrap_select_ok = true;

                                   std::printf(

                                       "BOOTSTRAP_SELECT ok=1 sender_uid=%s\n",

                                       ae::Format("{}", client.Load()->uid())

                                           .c_str());

                                   std::fflush(stdout);

                                   OnClientReady(std::move(client));

                                 });

}



static void FinishBootstrapPhase() {

  g_bootstrap_active = false;

  g_bootstrap_select_sub.Reset();

  g_bootstrap_write_sub.Reset();

  if (g_client.is_valid()) {

    StoreSenderUid(g_client.Load()->uid());

  }

  g_app->aether().Save();

  for (int i = 0; i < 400 && g_app; ++i) {

    auto const t = g_app->Update(ae::Now());

    g_app->WaitUntil(t);

    esp_task_wdt_reset();

  }

  ReleaseApp();

  g_boot_rtc.bootstrap_complete = 1;

  StoreBootRtc();

  std::printf("BOOTSTRAP_DONE sender_uid_stored=1\n");

  std::fflush(stdout);

  DeepSleep2s();

}



static void StartFullMessage() {

  std::printf("FULL_BEGIN service_uid=%s\n", SERVICE_UID);

  std::fflush(stdout);

  ConstructVanillaAether();

  g_select_sub = g_app->aether()

                     ->SelectClient(kParentUid, kBenchClientId)

                     .result_event()

                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {

                       if (!res) {

                         int const err = res.error();

                         std::printf("SELECT_RESULT ok=0 error=%d sender_uid=\n",

                                     err);

                         std::fflush(stdout);

                         MarkScenarioFail(

                             "select_client",

                             err == 3 ? "client_map_load_failed" : "select_error");

                         return;

                       }

                       auto client = std::move(res).value();

                       if (!client.Load()) {

                         std::printf("SELECT_RESULT ok=0 error=3 sender_uid=\n");

                         std::fflush(stdout);

                         MarkScenarioFail("select_client", "client_map_load_failed");

                         return;

                       }

                       g_pending_client = std::move(client);
                       g_select_ready_pending = true;

                     });

}



static bool HotSendAccepted(temp_sensor::prepared_send::HotSendStatus st) {

  return temp_sensor::prepared_send::HotSendConsumedNonce(st);

}



static void SendHotMessage() {

  if (g_scenario_failed) {

    StayAwakeForRunnerDump(g_dump_finish_reason);

  }

  if (!temp_sensor::prepared_send::HasPreparedSendBlock() ||

      temp_sensor::prepared_send::PreparedMessageLeft() == 0) {

    MarkScenarioFail("hot_prepared", "no_block");

    StayAwakeForRunnerDump(g_dump_finish_reason);

  }

  int const num = static_cast<int>(g_next_message);

  if (num == 2) {

    ResetHotRtcTable();

  }

  MarkHotAttemptStarted(num);

  std::printf("HOT_BEGIN number=%d hold_ms=%u\n", num,

              static_cast<unsigned>(kHotPostSendHoldMs));

#if defined(AE_SIMPLE_HOT_UDP_LWIP_B)
  std::printf("HOT_WIFI=b_lwip_cached\n");
#endif
  std::fflush(stdout);

  temp_sensor::prepared_send::FastSendResult result{};

#if defined(AE_SIMPLE_HOT_UDP_LWIP_B)
  if (!temp_sensor::prepared_send::PreparedWifiRtcCacheIsValid(g_wifi_cache)) {
    result.status = temp_sensor::prepared_send::HotSendStatus::kSendFailed;
    MarkScenarioFail("hot_wifi_cache", "invalid_rtc_cache");
    RecordHotAttempt(num, result);
    StayAwakeForRunnerDump(g_dump_finish_reason);
  }
  ae::DataBuffer packet{};
  auto const enc = temp_sensor::prepared_send::EncodePreparedPayload(
      Payload(num), &packet);
  result.encode_us = 1;
  if (enc != temp_sensor::prepared_send::HotSendStatus::kSent) {
    result.status = enc;
  } else {
    result.status_flags |= static_cast<std::uint8_t>(
        bench::BisectStatusBits::kEncodeOk);
    std::uint32_t dest_ip = 0;
    std::uint16_t dest_port = 0;
    if (!temp_sensor::prepared_send::PreparedEndpointIpv4(&dest_ip,
                                                          &dest_port)) {
      result.status = temp_sensor::prepared_send::HotSendStatus::kSendFailed;
    } else {
      temp_sensor::udp_cached_lwip::SendRequest req{};
      req.dest_ipv4 = dest_ip;
      req.dest_port = dest_port;
      req.payload = packet.data();
      req.payload_len = packet.size();
      req.post_send_hold_ms = static_cast<int>(kHotPostSendHoldMs);
      req.use_peer_static_arp = false;
      auto const sr = temp_sensor::udp_cached_lwip::SendOnce(
          WIFI_SSID, WIFI_PASSWORD, g_wifi_cache, req);
      result.got_ip_seen = sr.got_ip;
      result.sendto_ok = sr.udp_send_ok;
      result.sendto_begin_us = sr.udp_send_ok ? 1 : 0;
      result.status = sr.udp_send_ok
                          ? temp_sensor::prepared_send::HotSendStatus::kSent
                          : temp_sensor::prepared_send::HotSendStatus::
                                kSendFailed;
    }
  }
#else
  result = temp_sensor::prepared_send::SendPreparedOnceWithFastPath(
      VanillaHotWifi(), Payload(num), nullptr);
#endif

  RecordHotAttempt(num, result);

  std::printf(

      "HOT_RESULT number=%d status=%.*s sendto_ok=%u got_ip=%u\n", num,

      static_cast<int>(temp_sensor::prepared_send::ToString(result.status).size()),

      temp_sensor::prepared_send::ToString(result.status).data(),

      static_cast<unsigned>(result.sendto_ok),

      static_cast<unsigned>(result.got_ip_seen));

  std::fflush(stdout);

  if (!HotSendAccepted(result.status)) {

    MarkScenarioFail("hot_send", temp_sensor::prepared_send::ToString(result.status).data());

    StayAwakeForRunnerDump(g_dump_finish_reason);

  }

  ++g_next_message;

  g_hot_rtc.next_message = g_next_message;

  if (g_next_message > kLastMessage) {

    std::printf("SCENARIO_DONE\n");

    std::fflush(stdout);

    StayAwakeForRunnerDump("SCENARIO_DONE");

  }

  DeepSleep2s();

}



static void ColdBootUsbWindow() {

  auto const reset = esp_reset_reason();

  if (reset == ESP_RST_POWERON || reset == ESP_RST_BROWNOUT) {

    for (int i = 0; i < 60; ++i) {

      vTaskDelay(pdMS_TO_TICKS(1000));

      esp_task_wdt_reset();

    }

  }

}



void AppSetup() {

  nvs_flash_init();

  esp_event_loop_delete_default();

  std::printf("FULL_WIFI=vanilla\n");

  std::printf("HOT_WIFI=vanilla hold_ms=%u\n",

              static_cast<unsigned>(kHotPostSendHoldMs));

  std::printf("BOOT reset_reason=%d next_message=%u bootstrap=%u\n",

              static_cast<int>(esp_reset_reason()),

              static_cast<unsigned>(g_next_message),

              static_cast<unsigned>(ValidBootRtc(g_boot_rtc)

                                        ? g_boot_rtc.bootstrap_complete

                                        : 0));

  std::fflush(stdout);

  if (!ValidBootRtc(g_boot_rtc)) {

    g_boot_rtc = BootRtcState{};

    StoreBootRtc();

  }

  if (g_next_message == kHalted) {

    MarkScenarioFail("halted", "rtc_halted");

    ScenarioFailIdle();

  }

  ColdBootUsbWindow();

  if (g_boot_rtc.bootstrap_complete == 0) {

    StartBootstrapPhase();

    return;

  }

  if (g_scenario_failed) {

    ScenarioFailIdle();

  }

  std::printf("CLIENT_RELOAD_CHECK client_id=%s bootstrap=1\n", kBenchClientId);

  std::fflush(stdout);

  if (g_next_message == 1) {

    StartFullMessage();

    return;

  }

  SendHotMessage();

}



void AppLoop() {

  if (g_scenario_failed) {

    ScenarioFailIdle();

  }

  if (g_bootstrap_active && g_app) {

    if (g_bootstrap_select_done && !g_bootstrap_select_ok) {

      ScenarioFailIdle();

    }

    if (esp_timer_get_time() >= g_bootstrap_deadline_us && !g_bootstrap_write_terminal &&

        !(g_bootstrap_select_done && g_bootstrap_select_ok)) {

      MarkScenarioFail("bootstrap_select", "deadline");

      ScenarioFailIdle();

    }

    auto const t = g_app->Update(ae::Now());

    esp_task_wdt_reset();

    auto wait_until = t;

    auto const cap = ae::Now() + std::chrono::microseconds(

        std::max<std::int64_t>(0, g_bootstrap_deadline_us - esp_timer_get_time()));

    if (wait_until > cap) {

      wait_until = cap;

    }

    if (wait_until > ae::Now()) {

      g_app->WaitUntil(wait_until);

    }

    if (g_bootstrap_write_terminal) {

      if (g_bootstrap_write_status == ae::WriteAction::Status::kSuccess) {

        FinishBootstrapPhase();

      } else {

        MarkScenarioFail("bootstrap_write", WriteStatusName(g_bootstrap_write_status));

        ScenarioFailIdle();

      }

    }

    return;

  }

  if (g_next_message != 1 || !g_app) {

    return;

  }

  g_app->Update(ae::Now());

  esp_task_wdt_reset();

  if (g_select_ready_pending && g_pending_client) {
    g_select_ready_pending = false;
    std::printf("SELECT_RESULT ok=1 error=0 sender_uid=%s\n",
                ae::Format("{}", g_pending_client->uid()).c_str());
    std::fflush(stdout);
    OnClientReady(std::move(g_pending_client));
  }

  if (g_write_ok) {

    FinishFullPath();

  }

}



}  // namespace

}  // namespace temp_sensor



extern "C" void ae_exp_on_wifi_start(void) {

  esp_err_t const err = esp_wifi_set_ps(WIFI_PS_NONE);

  std::printf("FULL_WIFI_SET_PS err=%d name=%s\n", static_cast<int>(err),

              esp_err_to_name(err));

  std::fflush(stdout);

}



extern "C" void ae_exp_on_wifi_got_ip(void) {

  g_ae_exp_got_ip_flag = true;

  std::printf("GOT_IP=1\n");

  std::fflush(stdout);

}



void setup() { temp_sensor::AppSetup(); }



void loop() { temp_sensor::AppLoop(); }

