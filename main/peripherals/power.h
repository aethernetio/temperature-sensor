#pragma once

// Release retained LED and I2C pads before initialization after wake-up.
void peripherals_release_hold();

// Preserve the sensor rail only when ULP needs it during normal sleep.
void peripherals_off(bool keep_sensors = false);
// No automatic wake-up: restart with reset or a power cycle.
[[noreturn]] void battery_low_deep_sleep();
