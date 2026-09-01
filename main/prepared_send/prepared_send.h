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
#include "aether/wifi/wifi_probe_state.h"

namespace temp_sensor::prepared_send {

enum class HotSendStatus {
  kSent,
  // The whole datagram left the socket, so the prepared nonce is consumed and
  // the packet must never be resent, but the Wi-Fi TX-done callback never
  // reported success for it. The send is neither a failure nor a clean sample.
  kSentTxUnconfirmed,
  kNoPreparedBlock,
  kNonceExhausted,
  kEncodeFailed,
  kPersistFailed,
  kWifiFailed,
  kSendFailed,
  kUnsupported,
};

// True when the nonce was consumed, whatever the TX-done outcome was.
inline bool HotSendConsumedNonce(HotSendStatus status) {
  return status == HotSendStatus::kSent ||
         status == HotSendStatus::kSentTxUnconfirmed;
}

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

// Wait policy for the TX-done callback that follows sendto().
enum class FastTxDoneWaitMode : std::uint8_t {
  kFirstAny = 0,      // lab diagnostic: first callback, whatever its status
  kFirstSuccess = 1,  // lab diagnostic: first txStatus==true + 5 ms observe
  // Product: first txStatus==true for this datagram, then straight into the
  // POST hold. The 5 ms observe window of kFirstSuccess is diagnostic only and
  // would be charged to every production send.
  kFirstSuccessNoObserve = 2,
};

// Only a success callback that belongs to this datagram ends the wait.
inline bool FastTxDoneRequiresSuccess(FastTxDoneWaitMode mode) {
  return mode == FastTxDoneWaitMode::kFirstSuccess ||
         mode == FastTxDoneWaitMode::kFirstSuccessNoObserve;
}

// A duration that was never observed. Distinct from a measured zero.
static constexpr std::uint32_t kTimingMissing = 0xffffffffu;

struct FastPathConfig {
  bool use_bssid{false};
  bool use_channel{true};
  bool use_fast_scan{false};
  bool use_static_ip{true};
  bool use_static_arp{true};
  bool ampdu_tx_off{false};
  bool ampdu_rx_off{false};
  bool amsdu_tx_off{false};
  bool wifi_storage_ram{false};
  bool wifi_nvs_enable{true};
  bool force_ht20{false};
  // If true, wait up to AETHER_PREPARED_ARP_TIMEOUT_MS for gateway ARP when
  // static ARP is unused or failed.
  bool arp_wait_on_miss{false};
  // -1 = leave default; 0 = false; 1 = true
  std::int8_t dynamic_cs{-1};
  // 0 = use WIFI_INIT_CONFIG_DEFAULT values
  std::uint8_t static_rx_buf_num{0};
  std::uint8_t dynamic_rx_buf_num{0};
  std::uint8_t dynamic_tx_buf_num{0};
  std::uint8_t rx_ba_win{0};  // 0 = default; used when ampdu_rx enabled or F6
  FastAuthMode auth{FastAuthMode::kWpa3Both};
  std::uint8_t retry_max{10};
  std::uint16_t pre_delay_ms{200};
  std::uint16_t post_delay_ms{300};
  FastPostMode post_mode{FastPostMode::kFixedDelay};
  FastTxDoneWaitMode tx_done_wait{FastTxDoneWaitMode::kFirstAny};
  bool set_mac_retry_limit{false};
  std::uint8_t mac_short_retry{0};
  std::uint8_t mac_long_retry{0};
  // Safety wait for TX-done callback after sendto (ms). Default 100.
  std::uint16_t tx_done_timeout_ms{100};
  // false => WIFI_PS_NONE (reliability-first sleep campaign).
  bool ps_max_modem{false};
  bool fixed_1m{false};

  // Power-factor bench (ignored by production probe unless set).
  bool pmf_off{false};
  std::uint8_t connected_ps_mode{0};  // 0=NONE 1=MIN 2=MAX
  std::uint8_t listen_interval{1};
  std::uint8_t phase_ps{0};          // M0..M4
  bool encode_during_association{false};
  std::uint8_t teardown_policy{0};   // 0=full 1=stop 2=direct sleep
};

struct FastSendResult {
  HotSendStatus status{HotSendStatus::kWifiFailed};
  std::uint32_t cycle_us{0};
  std::uint32_t connect_us{0};
  std::uint32_t wifi_init_us{0};
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
  std::uint8_t diag_mode{0};
  std::uint8_t first_status{0xff};
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
  std::int16_t mac_retry_set_rc{-1};
  std::uint8_t mac_short_retry{0};
  std::uint8_t mac_long_retry{0};
  std::uint8_t mac_retry_called{0};
  std::uint32_t retry_cfg_us{0};
  std::uint32_t heap_before_wifi{0};
  std::uint32_t heap_after_wifi{0};
  std::uint8_t bssid[6]{};
  std::uint8_t sendto_ok{0};
  std::uint8_t sta_connected_seen{0};
  std::uint8_t got_ip_seen{0};
  std::uint8_t used_cached_channel{0};
  std::uint8_t channel_fallback_used{0};
  std::uint8_t used_static_ip{0};
  std::uint8_t dhcp_fallback_used{0};
  std::uint8_t used_static_arp{0};
  std::uint8_t arp_fallback_used{0};
  std::uint8_t fail_stage{0};  // 0=none 1=wifi 2=encode 3=sendto 4=other

  // Product metrics. Each is a plain duration of one step, not a delta against
  // sendto return, so they stay meaningful when the callback beats the syscall.
  // Set on every attempt that reached EncodePacket.
  std::uint32_t encode_us{0};
  std::uint32_t socket_create_us{0};
  std::uint32_t callback_register_us{0};
  // Absolute esp_timer microseconds within this boot, so the ordering of the
  // syscall and the callback can be audited afterwards.
  std::uint32_t sendto_begin_us{0};
  std::uint32_t sendto_return_us{0};
  std::uint32_t first_success_callback_us{0};
  std::uint32_t sendto_call_us{0};
  // sendto begin → first success callback. kTimingMissing when none arrived.
  std::uint32_t send_to_txdone_us{kTimingMissing};
  // Signed: the callback may run before sendto() returns.
  std::int32_t txdone_minus_sendto_return_us{0};
  // First success callback → start of the Wi-Fi teardown, i.e. the POST hold
  // as it actually happened. Zero when there was no success callback.
  std::uint32_t actual_post_us{0};
  std::uint8_t tx_done_confirmed{0};
  // esp_wifi_set_tx_done_cb() rejected the registration, so nothing about this
  // send's TX-done state can be trusted.
  std::uint8_t tx_cb_register_failed{0};
  std::int16_t tx_cb_register_rc{0};
};

// BASE = cached channel + static IPv4/netmask/gw + static ARP. No BSSID.
FastSendResult SendPreparedOnceWithFastPath(
    FastPathConfig const& cfg, ae::DataBuffer const& payload,
    BisectWifiCacheSnapshot const* wifi_cache = nullptr);

// Reliability-first prepared reconnect: channel→scan, static-IP→DHCP,
// static-ARP→ARP resolve (500 ms). Updates *wifi_cache on successful fallback.
FastSendResult SendPreparedOnceReliability(
    FastPathConfig const& cfg, ae::DataBuffer const& payload,
    BisectWifiCacheSnapshot* wifi_cache);

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

// Map adaptive WifiProbeProfile → FastPathConfig (BSSID never used).
FastPathConfig FastPathConfigForProbeProfile(ae::WifiProbeProfile profile,
                                             std::uint16_t pre_ms,
                                             std::uint16_t post_ms);

// Apply selected probe state into hot FastPath + cache snapshot helpers.
void ApplyProbeStateToHotConfig(ae::WifiProbeRtcState const& state,
                                FastPathConfig* cfg,
                                BisectWifiCacheSnapshot* cache);

// Apply power-bench variant options into FastPathConfig.
void ApplyPowerBenchToFastPath(FastPathConfig* cfg,
                               std::uint8_t teardown_policy,
                               bool pmf_off,
                               std::uint8_t connected_ps_mode,
                               std::uint8_t listen_interval,
                               std::uint8_t phase_ps,
                               bool encode_during_association);

// Hot-path failure: degrade selected profile and force P0 next.
void RecordHotProbeFailure(ae::WifiProbeRtcState* state,
                           ae::WifiProbeRecoveryReason reason);
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
