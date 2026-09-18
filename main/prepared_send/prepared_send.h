/*
 * Copyright 2026 Aethernet Inc.
 *
 * Prepared-send hot path for battery thermometer.
 *
 * Scope:
 * - full boot prepares/exports PreparedSendMessageBlock for the service stream;
 * - following ESP32 wakeups try to send one UDP prepared packet without
 *   constructing full AetherApp;
 * - any error falls back to normal full Aether boot;
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
// Temperature is in hundredths of a degree Celsius.
ae::DataBuffer MakeTemperaturePayload(std::int16_t temperature);

HotSendStatus TryHotWakePreparedSend(std::int16_t temperature);

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
