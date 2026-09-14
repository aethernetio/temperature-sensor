/*
 * Copyright 2026 Aethernet Inc.
 *
 * Thermometer 2 deep-sleep current bisect (AETHER_DIAG_SLEEP_POWER_BISECT).
 * Compile-time only; never linked into production builds.
 */

#ifndef TEMP_SENSOR_SLEEP_POWER_BISECT_H_
#define TEMP_SENSOR_SLEEP_POWER_BISECT_H_

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_SLEEP_POWER_BISECT)
extern "C" void RunSleepPowerBisect(void);
#endif

#endif  // TEMP_SENSOR_SLEEP_POWER_BISECT_H_
