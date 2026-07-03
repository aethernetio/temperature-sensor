/*
 * Copyright 2026 Aethernet Inc.
 *
 * Experimental prepared-send hot path for battery thermometer.
 *
 * Scope:
 * - full boot prepares/exports PreparedSendMessageBlock for the service stream;
 * - following ESP32 wakeups try to send one UDP prepared packet without
 *   constructing full AetherApp;
 * - any error falls back to normal full Aether boot.
 */

#ifndef TEMP_SENSOR_PREPARED_SEND_H_
#define TEMP_SENSOR_PREPARED_SEND_H_

#include <cstdint>
#include <cstddef>

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

char const* ToString(HotSendStatus status);

// Build the same binary temperature payload as SendValue().
ae::DataBuffer MakeTemperaturePayload(std::string temperature);

// Try the MCU hot path.
// Returns kSent only if:
//   - retained prepared block exists;
//   - Wi-Fi was connected;
//   - prepared packet was encoded;
//   - mutated block was persisted after nonce consumption;
//   - UDP datagram was sent.
HotSendStatus TryHotWakePreparedSend(std::string temperature);

// Export a new prepared block from the already initialized full Aether stream.
// Must be called only after full client/stream are usable.
bool ExportPreparedSendBlock(ae::AetherApp& app,
                             ae::P2pStream& stream,
                             std::size_t reserve_nonce_count);

void ClearPreparedSendBlock();
bool HasPreparedSendBlock();

}  // namespace temp_sensor::prepared_send

#endif  // TEMP_SENSOR_PREPARED_SEND_H_
