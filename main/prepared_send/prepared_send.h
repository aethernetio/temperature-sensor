/*
 * Copyright 2026 Aethernet Inc.
 *
 * Experimental prepared-send hot path for battery thermometer.
 *
 * PreparedSendMessageBlock is stored as a plain object in RTC RAM.
 * rtc_prepared_block_valid is the only presence indicator.
 */

#ifndef TEMP_SENSOR_PREPARED_SEND_H_
#define TEMP_SENSOR_PREPARED_SEND_H_

#include <cstddef>
#include <cstdint>

#include "aether/all.h"

namespace temp_sensor::prepared_send {

enum class HotSendStatus {
  kSent,
  kNoPreparedBlock,
  kNonceExhausted,
  kEncodeFailed,
  kWifiFailed,
  kSendFailed,
  kUnsupported,
};

char const* ToString(HotSendStatus status);

ae::DataBuffer MakeTemperaturePayload(std::int16_t temperature);

HotSendStatus TryHotWakePreparedSend(std::int16_t temperature);

bool ExportPreparedSendBlock(ae::AetherApp& app,
                             ae::P2pStream& stream,
                             std::size_t reserve_nonce_count);

void ClearPreparedSendBlock();
bool HasPreparedSendBlock();

}  // namespace temp_sensor::prepared_send

#endif  // TEMP_SENSOR_PREPARED_SEND_H_
