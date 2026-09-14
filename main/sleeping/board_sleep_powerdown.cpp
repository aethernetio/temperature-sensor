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

#include "sleeping/board_sleep_powerdown.h"

#if defined(ESP_PLATFORM)

#  include <driver/gpio.h>
#  include <esp_sleep.h>
#  include <hal/lp_core_ll.h>
#  include <soc/lp_aon_reg.h>

#  ifndef BOARD
#    define BOARD 0
#  endif
#  if BOARD == 0
#    include "boards/aether_esp32_c6.h"
#  elif BOARD == 4
#    include "boards/m5stack_atom_lite.h"
#  elif BOARD == 2
#    include "boards/nano_esp32_c6.h"
#  endif

#  ifndef STATUS_LED_ON_OFF_LEVEL
#    define STATUS_LED_ON_OFF_LEVEL 0
#  endif

static void HoldInputHighZ(gpio_num_t pin) {
  (void)gpio_hold_dis(pin);
  (void)gpio_reset_pin(pin);
  (void)gpio_set_direction(pin, GPIO_MODE_INPUT);
  (void)gpio_pullup_dis(pin);
  (void)gpio_pulldown_dis(pin);
  (void)gpio_hold_en(pin);
}

static void HoldOutputLevel(gpio_num_t pin, int level) {
  (void)gpio_hold_dis(pin);
  (void)gpio_reset_pin(pin);
  (void)gpio_set_direction(pin, GPIO_MODE_OUTPUT);
  (void)gpio_set_level(pin, level);
  (void)gpio_hold_en(pin);
}

extern "C" void BoardPowerDownForDeepSleep(void) {
  lp_core_ll_set_wakeup_source(0);
  lp_core_ll_request_sleep();
  REG_SET_BIT(LP_AON_LPCORE_REG, LP_AON_LPCORE_DISABLE);
  (void)esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ULP);

#  ifdef SENSOR_SDA_PIN
  HoldInputHighZ(SENSOR_SDA_PIN);
#  endif
#  ifdef SENSOR_SCL_PIN
  HoldInputHighZ(SENSOR_SCL_PIN);
#  endif

#  if defined(BOARD_HAS_PWR_ON) && BOARD_HAS_PWR_ON == 1 && \
      defined(PWR_ON_GPIO) && (PWR_ON_GPIO) >= 0
  HoldOutputLevel(static_cast<gpio_num_t>(PWR_ON_GPIO), 0);
#  endif

#  if defined(BOARD_HAS_LED) && BOARD_HAS_LED == 1 && defined(STATUS_LED_ON_PIN)
  HoldOutputLevel(STATUS_LED_ON_PIN, STATUS_LED_ON_OFF_LEVEL);
#  endif

#  if defined(BOARD_HAS_LED) && BOARD_HAS_LED == 1 && defined(STATUS_LED_PIN)
  HoldInputHighZ(STATUS_LED_PIN);
#  endif
}

#endif  // ESP_PLATFORM
