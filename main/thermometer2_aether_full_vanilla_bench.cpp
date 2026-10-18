/*
 * Thermometer 2: one full Æther P2pStream Write per timer wake, vanilla Wi-Fi,
 * 20 s deep sleep between cycles. No prepared/HOT/cache path.
 */

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>

#include "aether/all.h"
#include "aether/adapters/wifi_adapter.h"
#include "aether/ae_exp_wifi.h"
#include "aether/config.h"
#include "aether/env.h"
#include "aether/ae_exp_save_stats.h"
#include "bench_payload.h"
#include "sleeping/board_sleep_powerdown.h"

#if defined(ESP_PLATFORM)
#  include <esp_attr.h>
#  include <esp_event.h>
#  include <esp_random.h>
#  include <esp_sleep.h>
#  include <esp_wifi.h>
#  include <esp_system.h>
#  include <esp_timer.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <nvs_flash.h>
#  include <soc/soc_caps.h>

extern "C" std::uint64_t esp_rtc_get_time_us(void);
#endif

#ifndef BENCH_CLIENT_ID
#  define BENCH_CLIENT_ID "reliability_full_v1"
#endif
#ifndef AE_THERM_FULL_VANILLA_SLEEP_S
#  define AE_THERM_FULL_VANILLA_SLEEP_S 20
#endif
#ifndef AE_THERM_FULL_ACTIVE_TIMEOUT_MS
#  define AE_THERM_FULL_ACTIVE_TIMEOUT_MS 60000
#endif
#ifndef AE_THERM_FULL_MEASURE_CYCLES
#  define AE_THERM_FULL_MEASURE_CYCLES 100
#endif

namespace temp_sensor {
namespace {

static constexpr auto kParentUid =
    ae::Uid::FromString("b1ac52c8-8d94-bd39-4c01-a631ac594165");
static constexpr char const* kBenchClientId = BENCH_CLIENT_ID;

#if defined(SERVICE_UID)
static constexpr auto kServiceUid = ae::Uid::FromString(SERVICE_UID);
#else
static constexpr auto kServiceUid =
    ae::Uid::FromString("00000000-0000-0000-0000-000000000000");
#endif

static constexpr std::uint32_t kRtcMagic = 0x54465631u;  // TFV1
static constexpr std::uint16_t kRtcVersion = 2;
static constexpr std::uint64_t kSleepUs =
    static_cast<std::uint64_t>(AE_THERM_FULL_VANILLA_SLEEP_S) * 1000000ull;

enum class FailStage : std::uint8_t {
  kNone = 0,
  kTimeout,
  kSelectClient,
  kStream,
  kWriteNotCalled,
  kWriteTerminal,
  kSave,
};

struct RtcState {
  std::uint32_t magic;
  std::uint16_t version;
  std::uint16_t cycle;  // next cycle index (1-based after increment)
  std::uint32_t session;
  std::uint8_t measure_armed;
  std::uint8_t bootstrap_complete;
  std::uint8_t last_fail_stage;
  std::uint8_t pad;
  std::uint8_t sender_uid[16];
  std::uint32_t crc;
};

struct CycleJournalEntry {
  std::uint32_t run_id;
  std::uint8_t got_ip;
  std::uint8_t select_ok;
  std::uint8_t write_called;
  std::uint8_t write_status;
  std::uint8_t fail_stage;
  std::uint8_t pad;
  std::uint32_t active_ms;
};

#if defined(ESP_PLATFORM)
RTC_NOINIT_ATTR RtcState g_rtc{};
RTC_NOINIT_ATTR CycleJournalEntry g_journal[100]{};

static ae::WiFiInit MakeVanillaWifiInit() {
  ae::WiFiPowerSaveParam psp{};
  psp.wifi_ps_type = WIFI_PS_NONE;
  return ae::WiFiInit{
      std::vector<ae::WiFiAp>{{ae::WifiCreds{WIFI_SSID, WIFI_PASSWORD}, {}}},
      psp,
  };
}

std::shared_ptr<ae::AetherApp> g_app;
ae::Client::ptr g_client;
std::unique_ptr<ae::P2pStream> g_stream;
ae::Subscription g_select_sub;
ae::Subscription g_stream_sub;
ae::Subscription g_write_sub;

bool g_select_done = false;
bool g_select_ok = false;
int g_select_err = 0;
bool g_bootstrap_active = false;
bool g_write_armed = false;
bool g_write_terminal = false;
ae::WriteAction::Status g_write_status = ae::WriteAction::Status::kStop;
bool g_got_ip_logged = false;
bool g_cycle_active = false;
FailStage g_fail = FailStage::kNone;
std::int64_t g_active_t0_us = 0;
std::int64_t g_deadline_us = 0;
std::int64_t g_write_terminal_us = 0;
std::int64_t g_hold_end_us = 0;
std::int64_t g_save_begin_us = 0;
std::int64_t g_save_end_us = 0;

[[noreturn]] void IdleAfterCampaign();

void StageLog(char const* line) {
#if defined(AE_EXP_BENCH_STAGE) && AE_EXP_BENCH_STAGE
  std::printf("%s\n", line);
  std::fflush(stdout);
#else
  (void)line;
#endif
}

extern "C" void ae_exp_on_wifi_sta_connected(std::uint8_t /*channel*/) {
  StageLog("STA_CONNECTED");
}

extern "C" void ae_exp_on_wifi_got_ip(void) {
  StageLog("GOT_IP");
  g_got_ip_logged = true;
}

void LogJournalDumpEntry(int cycle_one_based, CycleJournalEntry const& e) {
  std::printf(
      "JOURNAL_DUMP cycle=%d run_id=%lu got_ip=%u select_ok=%u write_called=%u "
      "write_status=%u fail_stage=%u active_total_ms=%lu\n",
      cycle_one_based, static_cast<unsigned long>(e.run_id),
      static_cast<unsigned>(e.got_ip), static_cast<unsigned>(e.select_ok),
      static_cast<unsigned>(e.write_called),
      static_cast<unsigned>(e.write_status),
      static_cast<unsigned>(e.fail_stage),
      static_cast<unsigned long>(e.active_ms));
}

void DumpJournalAndIdle() {
  std::printf("JOURNAL_DUMP count=%d\n", AE_THERM_FULL_MEASURE_CYCLES);
  for (int i = 0; i < AE_THERM_FULL_MEASURE_CYCLES; ++i) {
    LogJournalDumpEntry(i + 1, g_journal[static_cast<std::size_t>(i)]);
  }
  std::fflush(stdout);
  IdleAfterCampaign();
}

void LogJournalRecapThrough(std::uint16_t through_cycle) {
  if (through_cycle == 0) {
    return;
  }
  auto const n = std::min<std::uint16_t>(
      through_cycle, static_cast<std::uint16_t>(AE_THERM_FULL_MEASURE_CYCLES));
  std::printf("JOURNAL_RECAP count=%u\n", static_cast<unsigned>(n));
  for (std::uint16_t c = 1; c <= n; ++c) {
    LogJournalDumpEntry(static_cast<int>(c),
                        g_journal[static_cast<std::size_t>(c - 1)]);
  }
  std::fflush(stdout);
}

std::uint32_t Crc32(void const* data, std::size_t len) {
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

bool ValidRtc(RtcState const& s) {
  if (s.magic != kRtcMagic || s.version != kRtcVersion) {
    return false;
  }
  RtcState tmp = s;
  tmp.crc = 0;
  return Crc32(&tmp, sizeof(tmp)) == s.crc;
}

void StoreRtc() {
  g_rtc.crc = 0;
  g_rtc.crc = Crc32(&g_rtc, sizeof(g_rtc));
}

void InitRtc(std::uint32_t session) {
  g_rtc = RtcState{};
  g_rtc.magic = kRtcMagic;
  g_rtc.version = kRtcVersion;
  g_rtc.cycle = 0;
  g_rtc.session = session;
  g_rtc.measure_armed = 0;
  g_rtc.bootstrap_complete = 0;
  std::memset(g_rtc.sender_uid, 0, sizeof(g_rtc.sender_uid));
  StoreRtc();
}

void LogBenchProfile() {
  std::printf("BENCH_MODE=FULL_VANILLA\n");
  std::printf("cycles=%d\n", AE_THERM_FULL_MEASURE_CYCLES);
  std::printf("sleep_s=%d\n", AE_THERM_FULL_VANILLA_SLEEP_S);
#if defined(SERVICE_UID)
  std::printf("service_uid=%s\n", SERVICE_UID);
#else
  std::printf("service_uid=UNKNOWN\n");
#endif
#if defined(AE_GIT_VERSION)
  std::printf("build_identifier=%s\n", AE_GIT_VERSION);
#else
  std::printf("build_identifier=UNKNOWN\n");
#endif
  std::printf("hot_prepared=0\n");
  std::fflush(stdout);
}

void LogBuildConfig() {
  std::printf(
      "BUILD_CFG registration=%d cloud_dns=%d tcp=%d udp=%d spifs=%d "
      "fs_init=%d\n",
#if AE_SUPPORT_REGISTRATION
      1,
#else
      0,
#endif
#if AE_SUPPORT_CLOUD_DNS
      1,
#else
      0,
#endif
#if AE_SUPPORT_TCP
      1,
#else
      0,
#endif
#if AE_SUPPORT_UDP
      1,
#else
      0,
#endif
#if AE_SUPPORT_SPIFS_FS
      1,
#else
      0,
#endif
#ifdef FS_INIT
      1
#else
      0
#endif
  );
  std::fflush(stdout);
}

void StoreSenderUid(ae::Uid const& uid) {
  std::memcpy(g_rtc.sender_uid, uid.value.data(), ae::Uid::kSize);
}

bool SenderUidMatchesRtc(ae::Uid const& uid) {
  if (g_rtc.bootstrap_complete == 0) {
    return true;
  }
  return std::memcmp(g_rtc.sender_uid, uid.value.data(), ae::Uid::kSize) == 0;
}

void OnClientReady(ae::Client::ptr client_ptr);
void LogWifiIfConnected();

char const* ResetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:
      return "POWERON";
    case ESP_RST_EXT:
      return "EXT";
    case ESP_RST_SW:
      return "SW";
    case ESP_RST_PANIC:
      return "PANIC";
    case ESP_RST_INT_WDT:
      return "INT_WDT";
    case ESP_RST_TASK_WDT:
      return "TASK_WDT";
    case ESP_RST_WDT:
      return "WDT";
    case ESP_RST_DEEPSLEEP:
      return "DEEPSLEEP";
    case ESP_RST_BROWNOUT:
      return "BROWNOUT";
    case ESP_RST_SDIO:
      return "SDIO";
    default:
      return "UNKNOWN";
  }
}

void PrepareRegisteredClientForWifi(ae::Client& client) {
  auto cloud = client.cloud().Load();
  if (!cloud) {
    std::printf("CLOUD_PREP cloud_load_failed=1\n");
    std::fflush(stdout);
    return;
  }
  for (auto& [sid, cs] : cloud->servers()) {
    auto server = cs.server.Load();
    if (!server) {
      continue;
    }
    auto const before = server->channels.size();
    server->RebuildChannelsFromAdapters();
    std::printf("CLOUD_PREP sid=%u channels %zu->%zu\n",
                static_cast<unsigned>(sid), before, server->channels.size());
    std::fflush(stdout);
  }
}

void LogWakeContext(char const* tag) {
  auto const r = esp_reset_reason();
  std::printf(
      "WAKE_CTX tag=%s reset=%d name=%s rtc_valid=%u bootstrap=%u cycle=%u "
      "measure_armed=%u\n",
      tag, static_cast<int>(r), ResetReasonName(r),
      static_cast<unsigned>(ValidRtc(g_rtc) ? 1 : 0),
      static_cast<unsigned>(g_rtc.bootstrap_complete),
      static_cast<unsigned>(g_rtc.cycle),
      static_cast<unsigned>(g_rtc.measure_armed));
  std::fflush(stdout);
}

void LogPhaseTimings(std::uint16_t cycle_index) {
  auto const now_us = esp_timer_get_time();
  auto const ms = [&](std::int64_t t0) -> unsigned long {
    if (t0 <= 0) {
      return 0;
    }
    return static_cast<unsigned long>((now_us - t0) / 1000);
  };
  std::printf(
      "PHASE cycle=%u write_to_hold_ms=%lu hold_ms=%lu save_ms=%lu "
      "active_to_sleep_ms=%lu hold_cfg_ms=10000 save_pump_ms=8000\n",
      static_cast<unsigned>(cycle_index),
      g_write_terminal_us > 0
          ? static_cast<unsigned long>((g_hold_end_us > 0 ? g_hold_end_us
                                                           : now_us) -
                                       g_write_terminal_us) /
                1000
          : 0UL,
      g_hold_end_us > 0 && g_write_terminal_us > 0
          ? static_cast<unsigned long>((g_hold_end_us - g_write_terminal_us) /
                                       1000)
          : 0UL,
      g_save_end_us > 0 && g_save_begin_us > 0
          ? static_cast<unsigned long>((g_save_end_us - g_save_begin_us) / 1000)
          : 0UL,
      ms(g_active_t0_us));
  std::fflush(stdout);
}

void LogStorageBackend(char const* tag) {
#if defined(FS_INIT)
  std::printf("AETHER_STORAGE tag=%s backend=FS_INIT static+rw\n", tag);
#else
#  if AE_SUPPORT_SPIFS_FS
  std::printf("AETHER_STORAGE tag=%s backend=SPIFFS registration=%d\n", tag,
#    if AE_SUPPORT_REGISTRATION
              1
#    else
              0
#    endif
  );
#  else
  std::printf("AETHER_STORAGE tag=%s backend=other\n", tag);
#  endif
#endif
  std::fflush(stdout);
}

void LogClientMap(char const* tag) {
  std::printf("AETHER_PROBE tag=%s client_id=%s select_pending=1\n", tag,
              kBenchClientId);
  std::fflush(stdout);
}

void SubscribeSelectClient() {
  g_select_sub = g_app->aether()
                     ->SelectClient(kParentUid, kBenchClientId)
                     .result_event()
                     .Subscribe([](ae::Result<ae::Client::ptr, int> res) {
                       g_select_done = true;
                       if (res) {
                         if (g_client.is_valid()) {
                           return;
                         }
                         g_select_ok = true;
                         g_select_err = 0;
                         auto uid = res.value()->uid();
                         if (!SenderUidMatchesRtc(uid)) {
                           g_select_ok = false;
                           g_select_err = -1;
                           std::printf("SELECT_CLIENT_RESULT ok=0 err=uid_mismatch\n");
                           std::fflush(stdout);
                           return;
                         }
                         std::printf("SELECT_CLIENT_RESULT ok=1 uid=%s err=0\n",
                                     ae::Format("{}", uid).c_str());
                         std::fflush(stdout);
                         OnClientReady(std::move(res).value());
                       } else {
                         g_select_err = res.error();
                         std::printf("SELECT_CLIENT_RESULT ok=0 err=%d\n",
                                     g_select_err);
                         std::fflush(stdout);
                       }
                     });
}

std::uint32_t MessageRunId() {
  return (g_rtc.session << 16) | (static_cast<std::uint32_t>(g_rtc.cycle) & 0xffffu);
}

void LogCycleJournal(std::uint16_t cycle_index) {
  if (cycle_index < 1 || cycle_index > AE_THERM_FULL_MEASURE_CYCLES) {
    return;
  }
  auto const idx = static_cast<std::size_t>(cycle_index - 1);
  auto const active_ms = static_cast<std::uint32_t>(
      (esp_timer_get_time() - g_active_t0_us) / 1000);
  g_journal[idx] = CycleJournalEntry{
      MessageRunId(),
      static_cast<std::uint8_t>(g_got_ip_logged ? 1 : 0),
      static_cast<std::uint8_t>(g_select_ok ? 1 : 0),
      static_cast<std::uint8_t>(g_write_armed ? 1 : 0),
      static_cast<std::uint8_t>(g_write_status),
      static_cast<std::uint8_t>(g_fail),
      0,
      active_ms,
  };
  std::printf(
      "JOURNAL cycle=%u run_id=%lu got_ip=%u select_ok=%u write_called=%u "
      "write_status=%u fail_stage=%u active_total_ms=%lu\n",
      static_cast<unsigned>(cycle_index),
      static_cast<unsigned long>(MessageRunId()),
      static_cast<unsigned>(g_got_ip_logged ? 1 : 0),
      static_cast<unsigned>(g_select_ok ? 1 : 0),
      static_cast<unsigned>(g_write_armed ? 1 : 0),
      static_cast<unsigned>(g_write_status),
      static_cast<unsigned>(g_fail),
      static_cast<unsigned long>(active_ms));
  std::fflush(stdout);
}

#if AE_DISTILLATION
static void ReleaseWifiDriversBeforeAppReset() {
  if (!g_app || !g_app->aether() || !g_app->aether()->adapter_registry) {
    return;
  }
  for (auto const& adapter : g_app->aether()->adapter_registry->adapters()) {
    ae::WifiAdapter::ptr wa{adapter};
    wa.WithLoaded([](auto const& a) {
      a->ReleaseOwnedDriverForPhaseHandoff();
    });
  }
}
#endif

void ReleaseApp() {
  g_select_sub.Reset();
  g_stream_sub.Reset();
  g_write_sub.Reset();
  g_stream.reset();
  g_client = {};
#if AE_DISTILLATION
  ReleaseWifiDriversBeforeAppReset();
#endif
  g_app.reset();
}

void ConstructAether() {
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
                  ctx.dns_resolver(), MakeVanillaWifiInit()));
            }
            return ap;
          })
#endif
  );
}

ae::TimePoint PumpDeadlineTimePoint() {
  auto const remain_us = g_deadline_us - esp_timer_get_time();
  if (remain_us <= 0) {
    return ae::Now();
  }
  return ae::Now() + std::chrono::microseconds(remain_us);
}

template <typename Pred>
void PumpUntil(Pred&& done) {
  while (!done()) {
    if (g_select_done && !g_select_ok) {
      return;
    }
    if (esp_timer_get_time() >= g_deadline_us) {
      return;
    }
    auto const now = ae::Now();
    auto const t = g_app->Update(now);
    LogWifiIfConnected();
    if (done()) {
      return;
    }
    auto wait_until = t;
    auto const cap = PumpDeadlineTimePoint();
    if (wait_until > cap) {
      wait_until = cap;
    }
    g_app->WaitUntil(wait_until);
  }
}

void LogWifiIfConnected() {
  if (g_got_ip_logged || !g_app || !g_app->aether()->adapter_registry) {
    return;
  }
  for (auto const& adapter : g_app->aether()->adapter_registry->adapters()) {
    ae::WifiAdapter::ptr wa{adapter};
    wa.WithLoaded([](auto const& a) {
      for (auto const& ap : a->access_points()) {
        ae::WifiAccessPoint::ptr wap{ap};
        wap.WithLoaded([&](auto const& p) {
          if (p && p->IsConnected()) {
            StageLog("GOT_IP");
            g_got_ip_logged = true;
          }
        });
      }
    });
  }
}

void MaybeWrite() {
  if (!g_stream || g_write_armed) {
    return;
  }
  if (!g_stream->stream_info().is_writable) {
    return;
  }
  temp_sensor::bench::ReliabilityPayload p{};
  p.type = static_cast<std::uint8_t>(
      temp_sensor::bench::ReliabilityMsgType::kFullReady);
  p.run_id = MessageRunId();
  p.seq = 0;
  auto payload = temp_sensor::bench::EncodeReliability<ae::DataBuffer>(p);
  g_write_armed = true;
  StageLog("WRITE_BEGIN");
  auto& wa = g_stream->Write(std::move(payload));
  g_write_sub = wa.status_event().Subscribe([](ae::WriteAction::Status st) {
    g_write_status = st;
    if (st == ae::WriteAction::Status::kSuccess ||
        st == ae::WriteAction::Status::kFail ||
        st == ae::WriteAction::Status::kStop) {
      g_write_terminal = true;
      g_write_terminal_us = esp_timer_get_time();
      std::printf("WRITE_TERMINAL status=%d run_id=%lu\n",
                  static_cast<int>(st),
                  static_cast<unsigned long>(MessageRunId()));
      std::fflush(stdout);
    }
  });
}

void PumpAppFor(std::int64_t hold_ms) {
  if (!g_app || hold_ms <= 0) {
    return;
  }
  auto const hold_until_us =
      esp_timer_get_time() + hold_ms * static_cast<std::int64_t>(1000);
  while (esp_timer_get_time() < hold_until_us) {
    if (esp_timer_get_time() >= g_deadline_us) {
      break;
    }
    auto const t = g_app->Update(ae::Now());
    auto wait_until = t;
    auto const cap = PumpDeadlineTimePoint();
    auto const hold_cap =
        ae::Now() +
        std::chrono::microseconds(hold_until_us - esp_timer_get_time());
    if (wait_until > cap) {
      wait_until = cap;
    }
    if (wait_until > hold_cap) {
      wait_until = hold_cap;
    }
    if (wait_until <= ae::Now()) {
      break;
    }
    g_app->WaitUntil(wait_until);
    LogWifiIfConnected();
  }
}

void HoldForCloudDelivery(std::int64_t hold_ms) {
  if (!g_app || hold_ms <= 0) {
    return;
  }
  auto const remain_ms = std::max<std::int64_t>(
      0, (g_deadline_us - esp_timer_get_time()) / 1000);
  PumpAppFor(std::min(hold_ms, remain_ms));
  g_hold_end_us = esp_timer_get_time();
}

void FlushAetherSave() {
  if (!g_app || !g_app->aether()) {
    return;
  }
  g_save_begin_us = esp_timer_get_time();
  ae::AeExpSaveSetCaller(ae::AeExpSaveCaller::kNone);
  g_app->aether().Save();
  PumpAppFor(8000);
  g_save_end_us = esp_timer_get_time();
  std::printf("SAVE_DONE save_pump_ms=%lu\n",
              static_cast<unsigned long>((g_save_end_us - g_save_begin_us) /
                                         1000));
  std::fflush(stdout);
}

void OnClientReady(ae::Client::ptr client_ptr) {
  if (g_client.is_valid()) {
    return;
  }
  g_client = std::move(client_ptr);
  auto client = g_client.Load();
  PrepareRegisteredClientForWifi(*client);
  auto handle = client->message_stream_manager().CreatePort(kServiceUid);
  g_stream = std::make_unique<ae::P2pStream>(*g_app, client, kServiceUid,
                                             std::move(handle));
  g_stream_sub =
      g_stream->stream_update_event().Subscribe([]() { MaybeWrite(); });
  StageLog("STREAM_STATE created");
  MaybeWrite();
}

void StartBootstrap() {
  g_bootstrap_active = true;
  g_select_done = false;
  g_select_ok = false;
  g_select_err = 0;
  g_write_armed = false;
  g_write_terminal = false;
  g_got_ip_logged = false;
  g_fail = FailStage::kNone;
  g_active_t0_us = esp_timer_get_time();
  g_deadline_us = g_active_t0_us + static_cast<std::int64_t>(120) * 1000000;
  StageLog("BOOTSTRAP_START");
  ConstructAether();
  LogBuildConfig();
  LogStorageBackend("bootstrap_after_construct");
  LogClientMap("bootstrap_after_construct");
  SubscribeSelectClient();
  g_cycle_active = true;
}

void StartCycle() {
  g_bootstrap_active = false;
  g_select_done = false;
  g_select_ok = false;
  g_select_err = 0;
  g_write_armed = false;
  g_write_terminal = false;
  g_got_ip_logged = false;
  g_fail = FailStage::kNone;
  g_active_t0_us = esp_timer_get_time();
  g_deadline_us =
      g_active_t0_us + static_cast<std::int64_t>(AE_THERM_FULL_ACTIVE_TIMEOUT_MS) * 1000;

  ++g_rtc.cycle;
  StoreRtc();

  std::printf("BOOT cycle=%u session=%lu run_id=%lu\n",
              static_cast<unsigned>(g_rtc.cycle),
              static_cast<unsigned long>(g_rtc.session),
              static_cast<unsigned long>(MessageRunId()));
  std::fflush(stdout);

  StageLog("WIFI_START");
  ConstructAether();
  LogStorageBackend("cycle_after_construct");
  LogClientMap("cycle_after_construct");
  SubscribeSelectClient();
  g_cycle_active = true;
}

void FinishBootstrapAndSleep() {
  StageLog("BOOTSTRAP_FINISH_BEGIN");
  g_cycle_active = false;
  g_bootstrap_active = false;
  FlushAetherSave();
  StageLog("BOOTSTRAP_SAVE");
  std::printf("RELEASE_APP_BEGIN tag=bootstrap\n");
  std::fflush(stdout);
  ReleaseApp();
  std::printf("RELEASE_APP_DONE tag=bootstrap\n");
  std::fflush(stdout);
  StageLog("NETWORK_STOPPED");
  BoardPrepareDeepSleep(1);
  esp_sleep_enable_timer_wakeup(kSleepUs);
  StoreRtc();
  std::printf("BOOTSTRAP_DONE sender_uid_stored=1\n");
  std::fflush(stdout);
  std::printf("DEEP_SLEEP_ENTER sleep_s=%d\n", AE_THERM_FULL_VANILLA_SLEEP_S);
  std::fflush(stdout);
  (void)esp_deep_sleep_try_to_start();
  esp_deep_sleep_start();
  std::printf("DEEP_SLEEP_FAILED tag=bootstrap\n");
  std::fflush(stdout);
  esp_restart();
}

void FinishCycleAndSleep() {
  g_cycle_active = false;
  FlushAetherSave();
  StageLog("STATE_SAVED");
  ReleaseApp();
  StageLog("NETWORK_STOPPED");
  LogPhaseTimings(g_rtc.cycle);
  LogCycleJournal(g_rtc.cycle);
  if (g_rtc.cycle == AE_THERM_FULL_MEASURE_CYCLES) {
    LogJournalRecapThrough(g_rtc.cycle);
  }
  BoardPrepareDeepSleep(1);
  esp_sleep_enable_timer_wakeup(kSleepUs);
  StoreRtc();
  std::printf("DEEP_SLEEP_ENTER sleep_s=%d\n", AE_THERM_FULL_VANILLA_SLEEP_S);
  std::fflush(stdout);
  (void)esp_deep_sleep_try_to_start();
  esp_deep_sleep_start();
  std::printf("DEEP_SLEEP_FAILED tag=cycle cycle=%u\n",
              static_cast<unsigned>(g_rtc.cycle));
  std::fflush(stdout);
  esp_restart();
}

[[noreturn]] void IdleAfterCampaign() {
  StageLog("CAMPAIGN_DONE");
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

#endif  // ESP_PLATFORM

}  // namespace
}  // namespace temp_sensor

#if defined(ESP_PLATFORM)

void setup() {
  using namespace temp_sensor;
  LogBenchProfile();
  std::printf("BOOT THERM2_FULL_VANILLA setup begin\n");
  std::fflush(stdout);
  nvs_flash_init();
  esp_event_loop_delete_default();
  auto const reset = esp_reset_reason();
  LogWakeContext("setup");
  if (reset == ESP_RST_POWERON || reset == ESP_RST_BROWNOUT) {
    StageLog("BOOT cold_power_on");
    StageLog("AWAKE_USB_WINDOW begin 60s");
    vTaskDelay(pdMS_TO_TICKS(60000));
    StageLog("AWAKE_USB_WINDOW end");
  } else if (reset == ESP_RST_DEEPSLEEP) {
    StageLog("BOOT timer_wake");
  } else {
    std::printf("BOOT unexpected_reset name=%s\n", ResetReasonName(reset));
    std::fflush(stdout);
  }

  if (!ValidRtc(g_rtc)) {
    InitRtc(static_cast<std::uint32_t>(esp_random()));
  }
  LogBuildConfig();

  if (g_rtc.cycle >= AE_THERM_FULL_MEASURE_CYCLES &&
      g_rtc.measure_armed != 0) {
    DumpJournalAndIdle();
  }

  if (g_rtc.bootstrap_complete == 0) {
    StartBootstrap();
    return;
  }

  if (g_rtc.measure_armed == 0) {
    g_rtc.measure_armed = 1;
    StoreRtc();
  }

  StartCycle();
}

void loop() {
  using namespace temp_sensor;
  if (!g_cycle_active || !g_app) {
    return;
  }

  if (g_bootstrap_active) {
    PumpUntil([]() {
      if (g_select_done && !g_select_ok) {
        return true;
      }
      return g_select_done && g_write_terminal;
    });
    if (g_select_done && !g_select_ok) {
      g_fail = FailStage::kSelectClient;
      std::printf("BOOTSTRAP_FAIL stage=select_client err=%d\n", g_select_err);
      std::fflush(stdout);
      IdleAfterCampaign();
    }
    if (esp_timer_get_time() >= g_deadline_us && !g_write_terminal) {
      std::printf("BOOTSTRAP_FAIL stage=timeout\n");
      std::fflush(stdout);
      IdleAfterCampaign();
    }
    if (g_write_terminal) {
      if (g_write_status == ae::WriteAction::Status::kSuccess) {
        HoldForCloudDelivery(10000);
        if (g_client.is_valid()) {
          auto const uid = g_client.Load()->uid();
          StoreSenderUid(uid);
          std::printf("SENDER_UID_RTC stored=%s\n",
                      ae::Format("{}", uid).c_str());
          std::fflush(stdout);
        }
        g_rtc.bootstrap_complete = 1;
        StoreRtc();
        FinishBootstrapAndSleep();
      } else {
        g_fail = FailStage::kWriteTerminal;
        std::printf("BOOTSTRAP_FAIL stage=write_terminal status=%d\n",
                    static_cast<int>(g_write_status));
        std::fflush(stdout);
        IdleAfterCampaign();
      }
    }
    return;
  }

  PumpUntil([]() {
    if (g_select_done && !g_select_ok) {
      return true;
    }
    return g_select_done && g_write_terminal;
  });
  LogWifiIfConnected();

  if (esp_timer_get_time() >= g_deadline_us && !g_write_terminal) {
    g_fail = FailStage::kTimeout;
    std::printf("CYCLE_FAIL stage=timeout cycle=%u\n",
                static_cast<unsigned>(g_rtc.cycle));
    std::fflush(stdout);
    FinishCycleAndSleep();
    return;
  }

  if (g_select_done && !g_select_ok) {
    g_fail = FailStage::kSelectClient;
    g_rtc.last_fail_stage = static_cast<std::uint8_t>(g_fail);
    StoreRtc();
    std::printf("CYCLE_FAIL stage=select_client err=%d cycle=%u\n",
                g_select_err, static_cast<unsigned>(g_rtc.cycle));
    std::fflush(stdout);
    FinishCycleAndSleep();
    return;
  }

  if (!g_write_armed) {
    g_fail = FailStage::kWriteNotCalled;
  } else if (!g_write_terminal) {
    return;
  } else if (g_write_status != ae::WriteAction::Status::kSuccess) {
    g_fail = FailStage::kWriteTerminal;
  }

  if (g_fail != FailStage::kNone) {
    g_rtc.last_fail_stage = static_cast<std::uint8_t>(g_fail);
    StoreRtc();
    std::printf("CYCLE_FAIL stage=%u cycle=%u\n",
                static_cast<unsigned>(g_rtc.last_fail_stage),
                static_cast<unsigned>(g_rtc.cycle));
    std::fflush(stdout);
    FinishCycleAndSleep();
    return;
  }

  HoldForCloudDelivery(10000);

  FinishCycleAndSleep();
}

#endif
