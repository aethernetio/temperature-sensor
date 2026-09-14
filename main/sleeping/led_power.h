#pragma once

#include "user_config.h"

#if defined(ESP_PLATFORM) && defined(STATUS_LED_ON_PIN)
#include "driver/gpio.h"
#include "esp_rom_gpio.h"
#include "esp_rom_serial_output.h"
#include "soc/gpio_sig_map.h"
#include <cstdio>
#endif

inline void ReleaseLedPowerHold() {
#if defined(ESP_PLATFORM) && defined(STATUS_LED_ON_PIN)
  // Boot restores the UART pin routing; release our retained low level so
  // the application console can work again after waking.
  ESP_ERROR_CHECK(gpio_hold_dis(STATUS_LED_ON_PIN));
#endif
}

inline void PowerOffLedForSleep() {
#if defined(ESP_PLATFORM) && defined(STATUS_LED_ON_PIN)
  // GPIO17 is also the default UART0 TX on this board. Drain the final logs
  // before taking its output away from UART and switching U6 off.
  std::fflush(stdout);
  std::fflush(stderr);
#if CONFIG_ESP_CONSOLE_UART
  esp_rom_output_tx_wait_idle(CONFIG_ESP_CONSOLE_UART_NUM);
#endif
  ESP_ERROR_CHECK(gpio_hold_dis(STATUS_LED_ON_PIN));
  // Release DIN as well, avoiding back-power through the unpowered LED.
  ESP_ERROR_CHECK(gpio_set_direction(STATUS_LED_PIN, GPIO_MODE_INPUT));
  ESP_ERROR_CHECK(gpio_set_pull_mode(STATUS_LED_PIN, GPIO_FLOATING));
  ESP_ERROR_CHECK(gpio_set_level(STATUS_LED_ON_PIN, 0));
  ESP_ERROR_CHECK(gpio_set_direction(STATUS_LED_ON_PIN, GPIO_MODE_OUTPUT));
  esp_rom_gpio_connect_out_signal(STATUS_LED_ON_PIN, SIG_GPIO_OUT_IDX, false, false);
  ESP_ERROR_CHECK(gpio_hold_en(STATUS_LED_ON_PIN));
#endif
}
