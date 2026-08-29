/*
 * Copyright 2026 Aethernet Inc.
 *
 * Compact binary benchmark payloads for prepared Wi-Fi experiments.
 * All multi-byte fields are little-endian.
 */

#ifndef TEMP_SENSOR_BENCH_PAYLOAD_H_
#define TEMP_SENSOR_BENCH_PAYLOAD_H_

#include <cstdint>
#include <cstring>
#include <vector>

namespace temp_sensor::bench {

static constexpr std::uint8_t kMagic = 0xAE;
static constexpr std::uint8_t kBisectMagic = 0xAF;

enum class MsgType : std::uint8_t {
  kFull = 1,
  kPrepared = 2,
  kFinal = 3,
};

enum class CacheFlags : std::uint8_t {
  kNone = 0,
  kUsedBssid = 1 << 0,
  kUsedStaticIp = 1 << 1,
  kDhcpSkipped = 1 << 2,
  kUsedStaticArp = 1 << 3,
  kArpFallback = 1 << 4,
  kWifiFallback = 1 << 5,
};

enum class BisectMsgType : std::uint8_t {
  kFull = 1,
  kPrepared = 2,
  kFinal = 3,
  kMeta = 4,
  kVariantSummary = 5,
};

enum class BisectVariant : std::uint8_t {
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

enum class BisectFactorBits : std::uint8_t {
  kBssid = 1 << 0,
  kChannel = 1 << 1,
  kFastScan = 1 << 2,
  kStaticIp = 1 << 3,
  kStaticArp = 1 << 4,
  kPsMaxModem = 1 << 5,
  kAmpduOff = 1 << 6,
  kFixed1M = 1 << 7,
};

enum class BisectStatusBits : std::uint8_t {
  kWifiReady = 1 << 0,
  kEncodeOk = 1 << 1,
  kSendtoOk = 1 << 2,
  kChannelMatch = 1 << 3,
};

#pragma pack(push, 1)
struct Payload {
  std::uint8_t magic{kMagic};
  std::uint8_t type{0};
  std::uint8_t outer_cycle{0};
  std::uint8_t prepared_index{0};
  std::uint16_t sequence_global{0};
  std::uint32_t registration_us{0};
  std::uint32_t previous_full_us{0};
  std::uint32_t previous_prepared_us{0};
  std::uint8_t cache_flags{0};
};

struct BisectPayload {
  std::uint8_t magic{kBisectMagic};
  std::uint8_t type{0};
  std::uint8_t variant_id{0};
  std::uint8_t prepared_index{0};
  std::uint16_t sequence_global{0};
  std::uint32_t time_us{0};
  std::uint32_t aux_us{0};
  std::uint8_t requested_channel{0};
  std::uint8_t actual_channel{0};
  std::uint8_t status_flags{0};
  std::uint8_t factor_bits{0};
  std::uint8_t wifi_ready_count{0};
  std::uint8_t encode_count{0};
  std::uint8_t sendto_count{0};
  std::uint8_t nonce_consumed{0};
  std::uint32_t cached_ip{0};
  std::uint8_t cached_bssid[6]{};
  std::uint8_t cached_channel{0};
  std::uint8_t pre_delay_ms{0};
};
#pragma pack(pop)

static constexpr std::uint8_t kFastMagic = 0xB1;

enum class FastMsgType : std::uint8_t {
  kFull = 1,
  kPrepared = 2,
  kFinal = 3,
};

enum class FastAssocBits : std::uint8_t {
  kBssid = 1 << 0,
  kChannel = 1 << 1,
  kFastScan = 1 << 2,
  kStaticIp = 1 << 3,
  kStaticArp = 1 << 4,
  kAmpduTxOff = 1 << 5,
  kStorageRam = 1 << 6,
  kCallback = 1 << 7,
};

#pragma pack(push, 1)
struct FastPayload {
  std::uint8_t magic{kFastMagic};
  std::uint8_t type{0};
  std::uint8_t test_id{0};
  std::uint8_t prepared_index{0};
  std::uint16_t sequence_global{0};
  std::uint32_t cycle_us{0};
  std::uint32_t connect_us{0};
  std::uint16_t pre_ms{0};
  std::uint16_t post_ms{0};
  std::uint8_t status_flags{0};
  std::uint8_t assoc_bits{0};
  std::uint8_t auth_negotiated{0};
  std::uint8_t retry_max{0};
  std::uint16_t wifi_ready_count{0};
  std::uint16_t encode_count{0};
  std::uint16_t sendto_count{0};
  std::uint16_t nonce_consumed{0};
  std::uint8_t cb_any{0};
  std::uint8_t cb_match{0};
  std::uint8_t cb_count{0};
  std::uint8_t post_mode{0};
  std::uint32_t encode_send_us{0};
  std::uint32_t tx_done_wait_us{0};
  std::uint32_t teardown_us{0};
  std::uint8_t cb_timeout{0};
  std::uint8_t reserved0{0};
  std::uint8_t reserved1{0};
  std::uint8_t reserved2{0};
};
#pragma pack(pop)

static_assert(sizeof(Payload) == 19, "bench payload size");
static_assert(sizeof(BisectPayload) == 34, "bisect payload size");
static_assert(sizeof(FastPayload) == 50, "fast payload size");

inline char const* BisectVariantName(std::uint8_t id) {
  switch (static_cast<BisectVariant>(id)) {
    case BisectVariant::kB0:
      return "B0";
    case BisectVariant::kB1:
      return "B1";
    case BisectVariant::kC1:
      return "C1";
    case BisectVariant::kC2:
      return "C2";
    case BisectVariant::kC3:
      return "C3";
    case BisectVariant::kC4:
      return "C4";
    case BisectVariant::kC5:
      return "C5";
    case BisectVariant::kC6:
      return "C6";
    case BisectVariant::kC7:
      return "C7";
    case BisectVariant::kC8:
      return "C8";
    case BisectVariant::kP1:
      return "P1";
    case BisectVariant::kP2:
      return "P2";
    case BisectVariant::kP3:
      return "P3";
    default:
      return "?";
  }
}

inline char const* BisectVariantChange(std::uint8_t id) {
  switch (static_cast<BisectVariant>(id)) {
    case BisectVariant::kB0:
      return "no cache, 0ms pre-delay";
    case BisectVariant::kB1:
      return "no cache, 200ms pre-delay";
    case BisectVariant::kC1:
      return "BSSID only";
    case BisectVariant::kC2:
      return "CHANNEL only";
    case BisectVariant::kC3:
      return "BSSID+CHANNEL";
    case BisectVariant::kC4:
      return "FAST_SCAN only";
    case BisectVariant::kC5:
      return "STATIC_IP only";
    case BisectVariant::kC6:
      return "STATIC_IP+ARP (dep C5)";
    case BisectVariant::kC7:
      return "BSSID+STATIC_IP";
    case BisectVariant::kC8:
      return "CHANNEL+STATIC_IP";
    case BisectVariant::kP1:
      return "PS_MAX_MODEM";
    case BisectVariant::kP2:
      return "AMPDU_OFF";
    case BisectVariant::kP3:
      return "FIXED_1M";
    default:
      return "?";
  }
}

inline std::vector<std::uint8_t> EncodeVec(Payload const& p) {
  std::vector<std::uint8_t> out(sizeof(Payload));
  std::memcpy(out.data(), &p, sizeof(Payload));
  return out;
}

template <typename Buffer>
inline Buffer Encode(Payload const& p) {
  Buffer out(sizeof(Payload));
  std::memcpy(out.data(), &p, sizeof(Payload));
  return out;
}

template <typename Buffer>
inline bool Decode(Buffer const& data, Payload& out) {
  if (data.size() < sizeof(Payload)) {
    return false;
  }
  std::memcpy(&out, data.data(), sizeof(Payload));
  return out.magic == kMagic;
}

template <typename Buffer>
inline Buffer EncodeBisect(BisectPayload const& p) {
  Buffer out(sizeof(BisectPayload));
  std::memcpy(out.data(), &p, sizeof(BisectPayload));
  return out;
}

template <typename Buffer>
inline bool DecodeBisect(Buffer const& data, BisectPayload& out) {
  if (data.size() < sizeof(BisectPayload)) {
    return false;
  }
  std::memcpy(&out, data.data(), sizeof(BisectPayload));
  return out.magic == kBisectMagic;
}

template <typename Buffer>
inline Buffer EncodeFast(FastPayload const& p) {
  Buffer out(sizeof(FastPayload));
  std::memcpy(out.data(), &p, sizeof(FastPayload));
  return out;
}

template <typename Buffer>
inline bool DecodeFast(Buffer const& data, FastPayload& out) {
  if (data.size() < sizeof(FastPayload)) {
    return false;
  }
  std::memcpy(&out, data.data(), sizeof(FastPayload));
  return out.magic == kFastMagic;
}

static constexpr std::uint8_t kDsMagic = 0xD5;

enum class DsMsgType : std::uint8_t {
  kFull = 1,
  kHot = 2,
  kFinal = 3,
  kRecovery = 4,
};

enum class DsFlags : std::uint8_t {
  kBrownout = 1 << 0,
  kCallbackSeen = 1 << 1,
  kCallbackTimeout = 1 << 2,
  kCacheValid = 1 << 3,
  kStateValid = 1 << 4,
};

enum class DsPendingKind : std::uint8_t {
  kNone = 0,
  kFull = 1,
  kHot = 2,
};

#pragma pack(push, 1)
struct DsPayload {
  std::uint8_t magic{kDsMagic};
  std::uint8_t type{0};
  std::uint8_t outer_cycle{0};
  std::uint8_t hot_index{0};
  std::uint16_t sequence_global{0};
  std::uint16_t record_id{0};
  std::uint8_t reset_reason{0};
  std::uint8_t wake_cause{0};
  std::uint8_t flags{0};
  std::uint8_t brownout_count{0};
  std::uint8_t unexpected_reset_count{0};
  std::uint8_t negotiated_auth{0};
  std::uint32_t requested_sleep_us{0};
  std::uint32_t sleep_elapsed_to_app_us{0};
  std::uint32_t sleep_to_app_overhead_us{0};
  std::uint32_t app_entry_esp_timer_us{0};
  std::uint32_t pending_user_cycle_us{0};
  std::uint32_t pending_wifi_cycle_us{0};
  std::uint32_t connect_us{0};
  std::uint32_t tx_done_wait_us{0};
  std::uint32_t teardown_us{0};
  std::uint16_t prepared_message_left{0};
  std::uint8_t pending_kind{0};
  std::uint8_t pending_outer{0};
  std::uint8_t pending_hot_index{0};
  std::uint8_t reserved{0};
};
#pragma pack(pop)

static_assert(sizeof(DsPayload) == 56, "ds payload size");

template <typename Buffer>
inline Buffer EncodeDs(DsPayload const& p) {
  Buffer out(sizeof(DsPayload));
  std::memcpy(out.data(), &p, sizeof(DsPayload));
  return out;
}

template <typename Buffer>
inline bool DecodeDs(Buffer const& data, DsPayload& out) {
  if (data.size() < sizeof(DsPayload)) {
    return false;
  }
  std::memcpy(&out, data.data(), sizeof(DsPayload));
  return out.magic == kDsMagic;
}

}  // namespace temp_sensor::bench

#endif  // TEMP_SENSOR_BENCH_PAYLOAD_H_
