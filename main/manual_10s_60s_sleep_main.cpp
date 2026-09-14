/*
 * Copyright 2026 Aethernet Inc.
 *
 * Manual Thermometer 2 deep-sleep current test.
 * One app_main: quiet GPIOs → 10 s active → deep sleep 60 s → repeat.
 * No Wi-Fi, Aether, ULP app, sensors, or diagnostic framework.
 */

#include <driver/gpio.h>
#include <esp_sleep.h>
#include <esp_task_wdt.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <hal/lp_core_ll.h>
#include <soc/lp_aon_reg.h>

extern "C" void app_main(void) {
  (void)esp_task_wdt_delete(nullptr);

  // Stop LP core (do not run ULP).
  lp_core_ll_set_wakeup_source(0);
  lp_core_ll_request_sleep();
  REG_SET_BIT(LP_AON_LPCORE_REG, LP_AON_LPCORE_DISABLE);

  // GPIO2 PWR_ON: output LOW + hold (sensor rail off).
  (void)gpio_hold_dis(GPIO_NUM_2);
  (void)gpio_reset_pin(GPIO_NUM_2);
  (void)gpio_set_direction(GPIO_NUM_2, GPIO_MODE_OUTPUT);
  (void)gpio_set_level(GPIO_NUM_2, 0);
  (void)gpio_hold_en(GPIO_NUM_2);

  // GPIO17 STATUS_LED_ON: output LOW + hold (LED rail off).
  (void)gpio_hold_dis(GPIO_NUM_17);
  (void)gpio_reset_pin(GPIO_NUM_17);
  (void)gpio_set_direction(GPIO_NUM_17, GPIO_MODE_OUTPUT);
  (void)gpio_set_level(GPIO_NUM_17, 0);
  (void)gpio_hold_en(GPIO_NUM_17);

  // GPIO18 STATUS_LED signal: input high-Z, no pulls, hold.
  (void)gpio_hold_dis(GPIO_NUM_18);
  (void)gpio_reset_pin(GPIO_NUM_18);
  (void)gpio_set_direction(GPIO_NUM_18, GPIO_MODE_INPUT);
  (void)gpio_pullup_dis(GPIO_NUM_18);
  (void)gpio_pulldown_dis(GPIO_NUM_18);
  (void)gpio_hold_en(GPIO_NUM_18);

  // GPIO6 SDA: input high-Z, no pulls, hold.
  (void)gpio_hold_dis(GPIO_NUM_6);
  (void)gpio_reset_pin(GPIO_NUM_6);
  (void)gpio_set_direction(GPIO_NUM_6, GPIO_MODE_INPUT);
  (void)gpio_pullup_dis(GPIO_NUM_6);
  (void)gpio_pulldown_dis(GPIO_NUM_6);
  (void)gpio_hold_en(GPIO_NUM_6);

  // GPIO7 SCL: input high-Z, no pulls, hold.
  (void)gpio_hold_dis(GPIO_NUM_7);
  (void)gpio_reset_pin(GPIO_NUM_7);
  (void)gpio_set_direction(GPIO_NUM_7, GPIO_MODE_INPUT);
  (void)gpio_pullup_dis(GPIO_NUM_7);
  (void)gpio_pulldown_dis(GPIO_NUM_7);
  (void)gpio_hold_en(GPIO_NUM_7);

  // Visible active window on PPK (ordinary FreeRTOS delay).
  vTaskDelay(pdMS_TO_TICKS(10000));

  // ESP32-C6 power domains: unused OFF (no RTC_DATA_ATTR retention).
  // C6 has no RTC_SLOW/FAST_MEM or CNNT PD enums in this IDF — omit them.
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_XTAL32K, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RC32K, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_RC_FAST, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_CPU, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_VDDSDIO, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_MODEM, ESP_PD_OPTION_OFF);
  (void)esp_sleep_pd_config(ESP_PD_DOMAIN_TOP, ESP_PD_OPTION_OFF);

  (void)esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
  esp_sleep_enable_timer_wakeup(60ULL * 1000000ULL);
  esp_deep_sleep_start();
}
