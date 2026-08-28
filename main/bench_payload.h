/*
 * Copyright 2026 Aethernet Inc.
 *
 * Compact binary benchmark payload for prepared Wi-Fi cache 5x20 experiment.
 * All multi-byte fields are little-endian.
 */

#ifndef TEMP_SENSOR_BENCH_PAYLOAD_H_
#define TEMP_SENSOR_BENCH_PAYLOAD_H_

#include <cstdint>
#include <cstring>
#include <vector>

namespace temp_sensor::bench {

static constexpr std::uint8_t kMagic = 0xAE;

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
#pragma pack(pop)

static_assert(sizeof(Payload) == 19, "bench payload size");

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

}  // namespace temp_sensor::bench

#endif  // TEMP_SENSOR_BENCH_PAYLOAD_H_
