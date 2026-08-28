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

// Last send cache usage bits (bench CacheFlags). Valid after SendPreparedOnce.
std::uint8_t LastSendCacheFlags();

#if defined(ESP_PLATFORM)
// No-sleep bench: AetherApp release may leave ESP-IDF Wi-Fi/netif up.
void ReleaseFullAetherWifiForHotPath();

// Invalidate retained BSSID/channel/IP/gateway-MAC cache.
void InvalidatePreparedWifiCache();

// Export Wi-Fi association + IP + gateway MAC from the still-active FULL path.
// Call before destroying Aether Wi-Fi so prepared #1 can use the fast path.
bool CapturePreparedWifiCacheFromActiveConnection();
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
