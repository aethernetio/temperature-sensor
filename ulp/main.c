/*
 * Copyright 2026 Aethernet Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "user_config.h"

#if BOARD_HAS_ULP == 1

#  include <stdint.h>
#  include <stdbool.h>

#  include "ulp_lp_core.h"
#  include "ulp_lp_core_i2c.h"
#  include "ulp_lp_core_utils.h"

#  include "sensors/sensors.h"
#  if BOARD_HAS_STCC4 == 1
extern volatile uint32_t stcc4_error;
#  endif

#  define nullptr ((void*)0)

// Variables in RTC memory (accessible from main.c)
volatile int32_t wakeup_temp_threshold;  // Threshold: XX.XX°C
volatile uint32_t wakeup_co2_threshold;  // Threshold: XXX ppm
volatile uint32_t wakeup_gas_threshold;  // Threshold: Gas
// Variables to store latest values

int16_t temperature;
uint32_t humidity;
uint32_t pressure;
uint32_t co2;
uint32_t gas_resistance;

volatile uint32_t can_start = 0;
volatile uint32_t sample_count = 0;
volatile uint32_t sample_in_progress = 0;
volatile uint32_t wakeup_enabled = 0;

int main(void) {
  while (can_start == 0) {
    asm("nop");
  }  // Waiting main CPU

  sample_in_progress = 1;
  ReadSensors(&temperature, &humidity, &pressure, &co2, &gas_resistance);
  asm volatile("fence rw, rw" ::: "memory");
  ++sample_count;
  sample_in_progress = 0;
#  if BOARD_HAS_STCC4 == 1
  if (stcc4_error != 0) {
    return 0;
  }
#  endif
  bool should_wakeup = false;
  if (temperature > wakeup_temp_threshold) {
    should_wakeup = true;
  }
  if (co2 > wakeup_co2_threshold) {
    should_wakeup = true;
  }
  if (gas_resistance > wakeup_gas_threshold) {
    should_wakeup = true;
  }

  if (wakeup_enabled && should_wakeup) {
    ulp_lp_core_wakeup_main_processor();
  }

  // Analogue of "deep sleep" for the LP core itself
  // ulp_lp_core_halt();
  // ulp_lp_core_stop_lp_core();

  return 0;
}

#else
int main(void) { return 0; }
#endif
