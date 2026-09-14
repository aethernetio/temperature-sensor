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
#  include <soc/soc_caps.h>

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

extern "C" void BoardApplyMinPowerDomains(void) {
#  if SOC_PM_SUPPORT_RTC_PERIPH_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_OFF);
#  endif
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL, ESP_PD_OPTION_OFF);
#  if SOC_PM_SUPPORT_XTAL32K_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL32K, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RC32K_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RC32K, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_RC_FAST_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RC_FAST, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_CPU_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_CPU, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_VDDSDIO_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_VDDSDIO, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_MODEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_MODEM, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_TOP_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_TOP, ESP_PD_OPTION_OFF);
#  endif
#  if SOC_PM_SUPPORT_CNNT_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_CNNT, ESP_PD_OPTION_OFF);
#  endif
}

extern "C" void BoardRetainRtcMemoryOn(void) {
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_ON);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_ON);
#  endif
}

extern "C" void BoardRetainRtcMemoryAuto(void) {
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_AUTO);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_AUTO);
#  endif
}

extern "C" void BoardPrepareDeepSleep(int retain_rtc_mem_on) {
  BoardPowerDownForDeepSleep();
  BoardApplyMinPowerDomains();
  if (retain_rtc_mem_on) {
    BoardRetainRtcMemoryOn();
  }
}

#endif  // ESP_PLATFORM
