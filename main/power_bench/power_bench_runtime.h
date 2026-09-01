/*
 * Copyright 2026 Aethernet Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

#ifndef TEMP_SENSOR_POWER_BENCH_RUNTIME_H_
#define TEMP_SENSOR_POWER_BENCH_RUNTIME_H_

#include "power_bench/power_bench_options.h"

namespace temp_sensor::power_bench {

// Apply CPU frequency / PM / DFS settings for the current boot.
bool ApplyRuntimeOptions(PowerBenchOptions const& bench);

// Configure PHY calibration mode after deep-sleep wake when supported.
void ApplyPhyCalibrationPolicy(PowerBenchOptions const& bench);

}  // namespace temp_sensor::power_bench

#endif  // TEMP_SENSOR_POWER_BENCH_RUNTIME_H_
