#include "peripherals/power.h"
#include "user_config.h"
#include "esp_rom_gpio.h"
#include "esp_rom_serial_output.h"
#include "soc/gpio_sig_map.h"
#include <cstdio>
#include "driver/gpio.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include <initializer_list>
#if BOARD_HAS_ULP == 1
#include "ulp_lp_core.h"
#include "driver/rtc_io.h"
#endif

namespace {
void power_off_wifi() {
  // Stop directly: a separate disconnect event can trigger auto-reconnect.
  // The prepared-send path may already have stopped/deinitialized Wi-Fi.
  auto error = esp_wifi_stop();
  if (error != ESP_OK && error != ESP_ERR_WIFI_NOT_INIT &&
      error != ESP_ERR_WIFI_NOT_STARTED) {
    ESP_LOGW("POWER", "Wi-Fi stop failed: %s", esp_err_to_name(error));
  }
  error = esp_wifi_deinit();
  if (error != ESP_OK && error != ESP_ERR_WIFI_NOT_INIT) {
    ESP_LOGW("POWER", "Wi-Fi deinit failed: %s", esp_err_to_name(error));
  }
}

void power_off_led() {
#if defined(ESP_PLATFORM) && defined(STATUS_LED_ON_PIN)
  // GPIO17 is also the default UART0 RX on ESP32-C6. Drain the final logs
  // before changing console pin configuration and switching U6 off.
  std::fflush(stdout);
  std::fflush(stderr);
#if CONFIG_ESP_CONSOLE_UART
  esp_rom_output_tx_wait_idle(CONFIG_ESP_CONSOLE_UART_NUM);
#endif
  ESP_ERROR_CHECK(gpio_hold_dis(STATUS_LED_ON_PIN));
  // Match TestPower: keep DIN low throughout deep sleep.
  ESP_ERROR_CHECK(gpio_set_level(STATUS_LED_PIN, 0));
  ESP_ERROR_CHECK(gpio_set_direction(STATUS_LED_PIN, GPIO_MODE_OUTPUT));
  ESP_ERROR_CHECK(gpio_set_pull_mode(STATUS_LED_PIN, GPIO_FLOATING));
  esp_rom_gpio_connect_out_signal(STATUS_LED_PIN, SIG_GPIO_OUT_IDX, false, false);
  ESP_ERROR_CHECK(gpio_hold_dis(STATUS_LED_PIN));
  ESP_ERROR_CHECK(gpio_set_level(STATUS_LED_PIN, 0));
  ESP_ERROR_CHECK(gpio_hold_en(STATUS_LED_PIN));
  ESP_ERROR_CHECK(gpio_set_level(STATUS_LED_ON_PIN, 0));
  ESP_ERROR_CHECK(gpio_set_direction(STATUS_LED_ON_PIN, GPIO_MODE_OUTPUT));
  esp_rom_gpio_connect_out_signal(STATUS_LED_ON_PIN, SIG_GPIO_OUT_IDX, false, false);
  ESP_ERROR_CHECK(gpio_hold_en(STATUS_LED_ON_PIN));
#endif
}


void hold_low(gpio_num_t pin) {
  ESP_ERROR_CHECK(gpio_set_level(pin, 0));
  ESP_ERROR_CHECK(gpio_set_direction(pin, GPIO_MODE_OUTPUT));
  ESP_ERROR_CHECK(gpio_set_pull_mode(pin, GPIO_FLOATING));
  ESP_ERROR_CHECK(gpio_hold_dis(pin));
  ESP_ERROR_CHECK(gpio_set_level(pin, 0));
  ESP_ERROR_CHECK(gpio_hold_en(pin));
}
}

void peripherals_release_hold() {
#if defined(STATUS_LED_ON_PIN)
  ESP_ERROR_CHECK(gpio_hold_dis(STATUS_LED_ON_PIN));
  ESP_ERROR_CHECK(gpio_hold_dis(STATUS_LED_PIN));
#endif
#if BOARD_HAS_PWR_ON == 1
  for (auto pin : {SENSOR_SDA_PIN, SENSOR_SCL_PIN}) {
    ESP_ERROR_CHECK(gpio_set_direction(pin, GPIO_MODE_INPUT));
    ESP_ERROR_CHECK(gpio_set_pull_mode(pin, GPIO_FLOATING));
    ESP_ERROR_CHECK(gpio_hold_dis(pin));
  }
#endif
}

void peripherals_off(bool keep_sensors) {
  power_off_wifi();
  power_off_led();
#if BOARD_HAS_PWR_ON == 1
  if (!keep_sensors) {
#if BOARD_HAS_ULP == 1
    ESP_ERROR_CHECK(rtc_gpio_deinit(static_cast<gpio_num_t>(PWR_ON_GPIO)));
    ESP_ERROR_CHECK(rtc_gpio_deinit(static_cast<gpio_num_t>(SENSOR_SDA_PIN)));
    ESP_ERROR_CHECK(rtc_gpio_deinit(static_cast<gpio_num_t>(SENSOR_SCL_PIN)));
#endif
    for (auto pin : {SENSOR_SDA_PIN, SENSOR_SCL_PIN}) {
      hold_low(pin);
    }
    hold_low(static_cast<gpio_num_t>(PWR_ON_GPIO));
  }
#endif
#if BOARD == BOARD_AETHER_ESP32_C6
  gpio_config_t battery = {};
  battery.pin_bit_mask = 1ULL << GPIO_NUM_3;
  battery.mode = GPIO_MODE_DISABLE;
  battery.pull_up_en = GPIO_PULLUP_DISABLE;
  battery.pull_down_en = GPIO_PULLDOWN_DISABLE;
  battery.intr_type = GPIO_INTR_DISABLE;
  ESP_ERROR_CHECK(gpio_config(&battery));
#endif
}

[[noreturn]] void battery_low_deep_sleep() {
#if BOARD_HAS_ULP == 1
  ulp_lp_core_stop();
#endif
  ESP_ERROR_CHECK(esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL));
  peripherals_off();
  esp_deep_sleep_start();
}
