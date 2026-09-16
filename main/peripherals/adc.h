#pragma once
#include <cstdint>

#ifndef BATTERY_LOW_VOLTAGE_X100
#define BATTERY_LOW_VOLTAGE_X100 250
#endif
#ifndef BATTERY_CHECK_INTERVAL_MS
#define BATTERY_CHECK_INTERVAL_MS 10000
#endif

// Call once from app_main; also starts the monitor task on the Aether board.
void init_adc();
// Calibrated volts * 100, or -1 on failure. No artificial upper clipping.
std::int16_t read_battery_voltage_x100();
