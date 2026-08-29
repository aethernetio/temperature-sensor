/*
 * Copyright 2026 Aethernet Inc.
 *
 * Experimental prepared-send hot path for battery thermometer.
 *
 * Scope:
 * - full boot prepares/exports PreparedSendMessageBlock for the service stream;
 * - following ESP32 wakeups try to send one UDP prepared packet without
 *   constructing full AetherApp;
 * - any error falls back to normal full Aether boot;
 * - no-sleep benchmarks call SendPreparedOnce() with the same encode/UDP path.
 */

#ifndef TEMP_SENSOR_PREPARED_SEND_H_
#define TEMP_SENSOR_PREPARED_SEND_H_

#include <cstdint>
#include <cstddef>
#include <string_view>

#include "aether/all.h"

namespace temp_sensor::prepared_send {

enum class HotSendStatus {
  kSent,
  kNoPreparedBlock,
  kNonceExhausted,
  kEncodeFailed,
  kPersistFailed,
  kWifiFailed,
  kSendFailed,
  kUnsupported,
};

std::string_view ToString(HotSendStatus status);

// Build the same binary temperature payload as SendValue().
ae::DataBuffer MakeTemperaturePayload(std::string const& temperature);

// UTF-8 benchmark payload: "FULL:0" / "PREPARED:N" (legacy E2E).
ae::DataBuffer MakeBenchPayload(std::string_view kind, int sequence);

// Shared encode + Wi-Fi + UDP + post-send hold + Wi-Fi runtime cleanup.
HotSendStatus SendPreparedOnce(ae::DataBuffer const& payload);

// Last send cache usage bits (bench CacheFlags). Valid after SendPreparedOnce /
// BeginPreparedWifiSession / SendPreparedPacketOnActiveWifi.
std::uint8_t LastSendCacheFlags();

#if defined(ESP_PLATFORM)
// Bench-only: keep one Wi-Fi association across many prepared UDP sends.
// Does not replace production SendPreparedOnce().
bool BeginPreparedWifiSession();
HotSendStatus SendPreparedPacketOnActiveWifi(ae::DataBuffer const& payload);
void EndPreparedWifiSession();
// Duration of the last successful BeginPreparedWifiSession() (µs).
std::uint32_t LastWifiSessionStartUs();

// No-sleep bench: AetherApp release may leave ESP-IDF Wi-Fi/netif up.
void ReleaseFullAetherWifiForHotPath();

// Invalidate retained BSSID/channel/IP/gateway-MAC cache.
void InvalidatePreparedWifiCache();

// Export Wi-Fi association + IP + gateway MAC from the still-active FULL path.
// Call before destroying Aether Wi-Fi so prepared #1 can use the fast path.
bool CapturePreparedWifiCacheFromActiveConnection();

// Bench-only single-factor Wi-Fi bisect (does not alter SendPreparedOnce).
enum class WifiBisectVariant : std::uint8_t {
  kB0 = 0,
  kB1,
  kC1,
  kC2,
  kC3,
  kC4,
  kC5,
  kC6,
  kC7,
  kC8,
  kP1,
  kP2,
  kP3,
  kCount,
};

struct BisectWifiCacheSnapshot {
  bool valid_bssid{false};
  bool valid_ip{false};
  bool valid_gw_mac{false};
  std::uint8_t bssid[6]{};
  std::uint8_t channel{0};
  std::uint32_t ip{0};
  std::uint32_t netmask{0};
  std::uint32_t gateway{0};
  std::uint8_t gw_mac[6]{};
};

struct BisectSendResult {
  HotSendStatus status{HotSendStatus::kWifiFailed};
  std::uint32_t total_us{0};
  std::uint8_t requested_channel{0};
  std::uint8_t actual_channel{0};
  std::uint8_t status_flags{0};
  std::uint8_t factor_bits{0};
  std::uint8_t pre_delay_ms{0};
};

bool FreezeBisectWifiCacheFromActiveConnection();
BisectWifiCacheSnapshot GetBisectWifiCacheSnapshot();

// One reconnect prepared send under a single-factor Wi-Fi config.
// Wi-Fi failure before EncodePacket does not consume a prepared nonce.
BisectSendResult SendPreparedOnceWithBisectFactor(
    WifiBisectVariant variant, ae::DataBuffer const& payload);

enum class FastAuthMode : std::uint8_t {
  kWpa3Both = 0,
  kWpa3H2eOnly = 1,
  kWpa2 = 2,
};

enum class FastPostMode : std::uint8_t {
  kFixedDelay = 0,
  kTxDoneCb = 1,
  kTxDoneCbPlus10 = 2,
  kTxDoneCbPlus25 = 3,
};

// Diagnostic wait policy for late TX-done callback (experiment only).
enum class FastTxDoneWaitMode : std::uint8_t {
  kFirstAny = 0,      // current production-like: first callback
  kFirstSuccess = 1,  // wait first txStatus==true + 5 ms observe
};

struct FastPathConfig {
  bool use_bssid{false};
  bool use_channel{true};
  bool use_fast_scan{false};
  bool use_static_ip{true};
  bool use_static_arp{true};
  bool ampdu_tx_off{false};
  bool wifi_storage_ram{false};
  FastAuthMode auth{FastAuthMode::kWpa3Both};
  std::uint8_t retry_max{10};
  std::uint16_t pre_delay_ms{200};
  std::uint16_t post_delay_ms{300};
  FastPostMode post_mode{FastPostMode::kFixedDelay};
  FastTxDoneWaitMode tx_done_wait{FastTxDoneWaitMode::kFirstAny};
  // Experiment-only: MAC short/long retry via esp_wifi_internal_set_retry_counter.
  // Association retry_max is independent and must stay unchanged.
  bool set_mac_retry_limit{false};
  std::uint8_t mac_short_retry{0};
  std::uint8_t mac_long_retry{0};
};

struct FastSendResult {
  HotSendStatus status{HotSendStatus::kWifiFailed};
  std::uint32_t cycle_us{0};
  std::uint32_t connect_us{0};
  std::uint32_t encode_send_us{0};
  std::uint32_t tx_done_wait_us{0};
  std::uint32_t teardown_us{0};
  std::uint8_t requested_channel{0};
  std::uint8_t actual_channel{0};
  std::uint8_t negotiated_auth{0};
  std::uint8_t status_flags{0};
  std::uint8_t cb_any{0};
  std::uint8_t cb_match{0};
  std::uint8_t cb_timeout{0};
  std::uint8_t cb_count{0};
  // TX-done diagnostics (experiment).
  std::uint8_t diag_mode{0};
  std::uint8_t first_status{0xff};  // 0xff none, 0 fail, 1 success
  std::uint8_t tx_cb_total{0};
  std::uint8_t tx_cb_success{0};
  std::uint8_t tx_cb_failed{0};
  std::uint8_t callbacks_after_success{0};
  std::uint32_t first_cb_delta_us{0xffffffffu};
  std::uint32_t first_success_delta_us{0xffffffffu};
  std::uint32_t first_failed_delta_us{0xffffffffu};
  std::uint32_t last_cb_delta_us{0xffffffffu};
  std::int8_t rssi{0};
  std::uint8_t ap_primary{0};
  std::uint8_t disconnect_count{0};
  std::uint8_t last_disconnect_reason{0};
  std::uint8_t reconnect_count{0};
  // MAC retry-limit diagnostics (experiment).
  std::int16_t mac_retry_set_rc{-1};  // -1 = not called
  std::uint8_t mac_short_retry{0};
  std::uint8_t mac_long_retry{0};
  std::uint8_t mac_retry_called{0};
  std::uint32_t retry_cfg_us{0};
};

// BASE = cached channel + static IPv4/netmask/gw + static ARP. No BSSID.
// Wi-Fi 4, WIFI_PS_NONE, auto PHY rate, max TX power. Timer excludes 1 s gap.
// Optional wifi_cache overrides the in-RAM bisect cache (for deep-sleep RTC).
FastSendResult SendPreparedOnceWithFastPath(
    FastPathConfig const& cfg, ae::DataBuffer const& payload,
    BisectWifiCacheSnapshot const* wifi_cache = nullptr);

// RTC-retained Wi-Fi cache for deep-sleep experiments (not BSSID reconnect).
struct PreparedWifiRtcCache {
  std::uint32_t magic{0};
  std::uint16_t version{0};
  std::uint16_t flags{0};  // bit0=ip, bit1=channel, bit2=gw_mac, bit3=bssid_diag
  std::uint8_t channel{0};
  std::uint8_t bssid[6]{};
  std::uint8_t gw_mac[6]{};
  std::uint32_t ip{0};
  std::uint32_t netmask{0};
  std::uint32_t gateway{0};
  std::uint32_t crc{0};
};

static constexpr std::uint32_t kPreparedWifiRtcMagic = 0x57434631u;  // WCF1
static constexpr std::uint16_t kPreparedWifiRtcVersion = 1;

bool CapturePreparedWifiRtcCache(PreparedWifiRtcCache* out);
bool PreparedWifiRtcCacheIsValid(PreparedWifiRtcCache const& cache);
BisectWifiCacheSnapshot SnapshotFromPreparedWifiRtcCache(
    PreparedWifiRtcCache const& cache);
#endif

HotSendStatus TryHotWakePreparedSend(std::string const& temperature);

bool ExportPreparedSendBlock(ae::Client::ptr const& client, ae::Uid destination,
                             std::size_t reserve_message_count);

bool HasPreparedSendBlock();
std::uint32_t PreparedMessageLeft();

struct WiFiBaseStation {
  uint8_t target_bssid[6];
  uint8_t target_channel;
};
}  // namespace temp_sensor::prepared_send

#endif  // TEMP_SENSOR_PREPARED_SEND_H_
