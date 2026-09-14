/*
 * Copyright 2026 Aethernet Inc.
 *
 * Thermometer 2 deep-sleep current factor bisect.
 *
 * Flash windows (mandatory for PPK power-cycle COM recovery):
 *   - Cold power-on (wakeup != TIMER): stay awake ~25 s, then deep sleep
 *   - Flash/reset (non-timer wake): stay awake ~8 s, then deep sleep
 *   - Timer deep-sleep wake: short settle then deep sleep again
 *
 * Variant selected by AE_SLEEP_BISECT_VARIANT (compile-time string).
 * No Wi-Fi / Aether / sensors / LED drivers / ULP app load.
 */

#include "sleep_power_bisect.h"

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_SLEEP_POWER_BISECT)

#  include <cstdint>
#  include <cstring>

#  include <driver/gpio.h>
#  include <esp_sleep.h>
#  include <esp_system.h>
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>
#  include <hal/lp_core_ll.h>
#  include <soc/lp_aon_reg.h>
#  include <soc/soc_caps.h>

#  ifndef BOARD
#    define BOARD 0
#  endif
#  include "boards/aether_esp32_c6.h"

#  ifndef AE_SLEEP_BISECT_VARIANT
#    define AE_SLEEP_BISECT_VARIANT "M0_MINIMAL"
#  endif

#  ifndef AE_SLEEP_BISECT_COLD_MS
#    define AE_SLEEP_BISECT_COLD_MS 25000
#  endif
#  ifndef AE_SLEEP_BISECT_FLASH_MS
#    define AE_SLEEP_BISECT_FLASH_MS 8000
#  endif
#  ifndef AE_SLEEP_BISECT_TIMER_SETTLE_MS
#    define AE_SLEEP_BISECT_TIMER_SETTLE_MS 200
#  endif
#  ifndef AE_SLEEP_BISECT_SLEEP_US
#    define AE_SLEEP_BISECT_SLEEP_US (60ULL * 60ULL * 1000000ULL)
#  endif

// STATUS_LED_ON_PIN OFF polarity is unverified in schematic/driver.
// Variants explicitly test LOW vs HIGH; do not treat either as fact.
#  ifndef STATUS_LED_ON_OFF_LEVEL
#    define STATUS_LED_ON_OFF_LEVEL 0
#  endif

namespace {

bool VariantIs(char const* name) {
  return std::strcmp(AE_SLEEP_BISECT_VARIANT, name) == 0;
}

bool VariantHasPrefix(char const* prefix) {
  return std::strncmp(AE_SLEEP_BISECT_VARIANT, prefix, std::strlen(prefix)) ==
         0;
}

void HoldInputHighZ(gpio_num_t pin) {
  (void)gpio_hold_dis(pin);
  (void)gpio_reset_pin(pin);
  (void)gpio_set_direction(pin, GPIO_MODE_INPUT);
  (void)gpio_pullup_dis(pin);
  (void)gpio_pulldown_dis(pin);
  (void)gpio_hold_en(pin);
}

void HoldOutputLevel(gpio_num_t pin, int level) {
  (void)gpio_hold_dis(pin);
  (void)gpio_reset_pin(pin);
  (void)gpio_set_direction(pin, GPIO_MODE_OUTPUT);
  (void)gpio_set_level(pin, level);
  (void)gpio_hold_en(pin);
}

void StopLpCoreBestEffort() {
  lp_core_ll_set_wakeup_source(0);
  lp_core_ll_request_sleep();
  REG_SET_BIT(LP_AON_LPCORE_REG, LP_AON_LPCORE_DISABLE);
  (void)esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ULP);
}

void ApplyMinPowerDomains() {
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

void ApplyRtcMemOn() {
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_ON);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_ON);
#  endif
}

void ApplyRtcMemAuto() {
#  if SOC_PM_SUPPORT_RTC_SLOW_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_SLOW_MEM, ESP_PD_OPTION_AUTO);
#  endif
#  if SOC_PM_SUPPORT_RTC_FAST_MEM_PD
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_FAST_MEM, ESP_PD_OPTION_AUTO);
#  endif
}

void ApplyGpioFactors() {
  // Cumulative sets by letter prefix: M0; A*; B*; ...  Names are B1_*, not B_*.
  bool const pwr_low =
      VariantIs("A1_PWR_LOW") || VariantIs("A2_PWR_HOLD") ||
      VariantIs("A3_PWR_ISOLATE") || VariantHasPrefix("B") ||
      VariantHasPrefix("C") || VariantHasPrefix("D") ||
      VariantHasPrefix("E") || VariantHasPrefix("F") ||
      VariantHasPrefix("G") || VariantIs("Z_FULL_QUIET") ||
      VariantIs("PROD_QUIET");
  bool const pwr_high = VariantIs("H1_PWR_HIGH");
  bool const pwr_hold =
      VariantIs("A2_PWR_HOLD") || VariantIs("A3_PWR_ISOLATE") ||
      VariantIs("H1_PWR_HIGH") || VariantHasPrefix("B") ||
      VariantHasPrefix("C") || VariantHasPrefix("D") ||
      VariantHasPrefix("E") || VariantHasPrefix("F") ||
      VariantHasPrefix("G") || VariantIs("Z_FULL_QUIET") ||
      VariantIs("PROD_QUIET");
  bool const led_off_low =
      VariantIs("B1_LED_ON_LOW") || VariantIs("B3_LED_DATA_HZ") ||
      VariantHasPrefix("C") || VariantHasPrefix("D") ||
      VariantHasPrefix("E") || VariantHasPrefix("F") ||
      VariantHasPrefix("G") || VariantIs("Z_FULL_QUIET") ||
      VariantIs("PROD_QUIET");
  bool const led_off_high = VariantIs("B2_LED_ON_HIGH");
  bool const led_data_hz =
      VariantIs("B3_LED_DATA_HZ") || VariantHasPrefix("C") ||
      VariantHasPrefix("D") || VariantHasPrefix("E") ||
      VariantHasPrefix("F") || VariantHasPrefix("G") ||
      VariantIs("Z_FULL_QUIET") || VariantIs("PROD_QUIET");
  bool const i2c_hz =
      VariantIs("C1_I2C_HZ") || VariantHasPrefix("D") ||
      VariantHasPrefix("E") || VariantHasPrefix("F") ||
      VariantHasPrefix("G") || VariantIs("Z_FULL_QUIET") ||
      VariantIs("PROD_QUIET");
  bool const ulp_stop =
      VariantIs("D1_ULP_STOP") || VariantHasPrefix("E") ||
      VariantHasPrefix("F") || VariantHasPrefix("G") ||
      VariantIs("Z_FULL_QUIET") || VariantIs("PROD_QUIET");

  if (ulp_stop) {
    StopLpCoreBestEffort();
  }

  if (i2c_hz) {
    HoldInputHighZ(SENSOR_SDA_PIN);
    HoldInputHighZ(SENSOR_SCL_PIN);
  }

  if (pwr_low || pwr_high) {
#  if BOARD_HAS_PWR_ON == 1
    int const level = pwr_high ? 1 : 0;
    if (pwr_hold) {
      HoldOutputLevel(static_cast<gpio_num_t>(PWR_ON_GPIO), level);
    } else {
      (void)gpio_hold_dis(static_cast<gpio_num_t>(PWR_ON_GPIO));
      (void)gpio_reset_pin(static_cast<gpio_num_t>(PWR_ON_GPIO));
      (void)gpio_set_direction(static_cast<gpio_num_t>(PWR_ON_GPIO),
                               GPIO_MODE_OUTPUT);
      (void)gpio_set_level(static_cast<gpio_num_t>(PWR_ON_GPIO), level);
    }
#  endif
  }

  if (VariantIs("A3_PWR_ISOLATE") || VariantIs("Z_FULL_QUIET")) {
#  if BOARD_HAS_PWR_ON == 1
    // C6: hold + high-Z on unused pads is the practical isolate equivalent.
    HoldInputHighZ(static_cast<gpio_num_t>(PWR_ON_GPIO));
    // Re-drive LOW+hold after isolate attempt so rail stays off.
    HoldOutputLevel(static_cast<gpio_num_t>(PWR_ON_GPIO), 0);
#  endif
  }

#  if BOARD_HAS_LED == 1 && defined(STATUS_LED_ON_PIN)
  if (led_off_low) {
    HoldOutputLevel(STATUS_LED_ON_PIN, 0);
  } else if (led_off_high) {
    HoldOutputLevel(STATUS_LED_ON_PIN, 1);
  }
#  endif

#  if BOARD_HAS_LED == 1 && defined(STATUS_LED_PIN)
  if (led_data_hz) {
    HoldInputHighZ(STATUS_LED_PIN);
  }
#  endif
}

void ApplyPdFactors() {
  if (VariantIs("M0_MINIMAL") || VariantIs("A0_BASELINE_NOPIN") ||
      VariantHasPrefix("A") || VariantHasPrefix("B") ||
      VariantHasPrefix("C") || VariantHasPrefix("D") ||
      VariantIs("E1_PD_MIN") || VariantIs("F1_SILENT") ||
      VariantIs("G1_NO_EXTRA") || VariantIs("Z_FULL_QUIET")) {
    if (VariantIs("E2_RTC_MEM_ON")) {
      ApplyMinPowerDomains();
      ApplyRtcMemOn();
      return;
    }
    if (VariantIs("E3_RTC_MEM_AUTO")) {
      ApplyMinPowerDomains();
      ApplyRtcMemAuto();
      return;
    }
    ApplyMinPowerDomains();
  }
}

void FlashWindowThenSleep() {
  esp_reset_reason_t const rr = esp_reset_reason();
  esp_sleep_wakeup_cause_t const cause = esp_sleep_get_wakeup_cause();

  int wait_ms = AE_SLEEP_BISECT_FLASH_MS;
  if (cause == ESP_SLEEP_WAKEUP_TIMER) {
    wait_ms = AE_SLEEP_BISECT_TIMER_SETTLE_MS;
  } else if (rr == ESP_RST_POWERON || rr == ESP_RST_BROWNOUT) {
    // Cold power-on after PPK OFF→ON: long flashable window.
    wait_ms = AE_SLEEP_BISECT_COLD_MS;
  } else {
    // Flash / external / software / USB reset: shorter window.
    wait_ms = AE_SLEEP_BISECT_FLASH_MS;
  }

  if (wait_ms > 0) {
    vTaskDelay(pdMS_TO_TICKS(wait_ms));
  }

  ApplyGpioFactors();
  ApplyPdFactors();
  esp_sleep_enable_timer_wakeup(AE_SLEEP_BISECT_SLEEP_US);
  esp_deep_sleep_start();
}

}  // namespace

extern "C" void RunSleepPowerBisect(void) { FlashWindowThenSleep(); }

#endif  // ESP_PLATFORM && AETHER_DIAG_SLEEP_POWER_BISECT
