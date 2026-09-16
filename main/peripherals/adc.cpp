#include "peripherals/adc.h"
#include "peripherals/power.h"
#include "user_config.h"
#include "esp_log.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cstdio>

namespace {
constexpr char TAG[] = "BATTERY";
adc_oneshot_unit_handle_t adc_handle = nullptr;
adc_cali_handle_t cali_handle = nullptr;
void battery_task(void*) {
  for (;;) {
    const auto voltage = read_battery_voltage_x100();
    if (voltage >= 0 && voltage < BATTERY_LOW_VOLTAGE_X100) {
      std::puts("Battery low");
      std::fflush(stdout);
      battery_low_deep_sleep();
    }
    vTaskDelay(pdMS_TO_TICKS(BATTERY_CHECK_INTERVAL_MS));
  }
}
}

void init_adc() {
#if BOARD == BOARD_AETHER_ESP32_C6
  if (adc_handle) return;
  static_assert(BATTERY_CHECK_INTERVAL_MS >= portTICK_PERIOD_MS);
  static_assert(BATTERY_LOW_VOLTAGE_X100 > 0);
  adc_oneshot_unit_init_cfg_t unit = {};
  unit.unit_id = ADC_UNIT_1;
  unit.ulp_mode = ADC_ULP_MODE_DISABLE;
  ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit, &adc_handle));
  adc_oneshot_chan_cfg_t channel = {};
  channel.atten = ADC_ATTEN_DB_12;
  channel.bitwidth = ADC_BITWIDTH_12;
  ESP_ERROR_CHECK(adc_oneshot_config_channel(adc_handle, ADC_CHANNEL_3, &channel));
  adc_cali_curve_fitting_config_t calibration = {};
  calibration.unit_id = ADC_UNIT_1;
  calibration.chan = ADC_CHANNEL_3;
  calibration.atten = ADC_ATTEN_DB_12;
  calibration.bitwidth = ADC_BITWIDTH_12;
  ESP_ERROR_CHECK(adc_cali_create_scheme_curve_fitting(&calibration, &cali_handle));
  ESP_ERROR_CHECK(xTaskCreate(battery_task, "battery", 3072, nullptr, 5, nullptr)
                      == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
#endif
}

std::int16_t read_battery_voltage_x100() {
  if (!adc_handle || !cali_handle) return -1;
  int sum = 0;
  for (int i = 0; i < 10; ++i) {
    int raw = 0;
    const auto error = adc_oneshot_read(adc_handle, ADC_CHANNEL_3, &raw);
    if (error != ESP_OK) {
      ESP_LOGW(TAG, "ADC read failed: %s", esp_err_to_name(error));
      return -1;
    }
    sum += raw;
  }
  int millivolts = 0;
  if (adc_cali_raw_to_voltage(cali_handle, sum / 10, &millivolts) != ESP_OK) {
    ESP_LOGW(TAG, "ADC calibration conversion failed");
    return -1;
  }
  return static_cast<std::int16_t>(millivolts / 10);
}
