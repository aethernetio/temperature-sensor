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

#if defined ESP_PLATFORM && ULP_COMP == 0
#  include <freertos/FreeRTOS.h>
#  include <freertos/task.h>

#  include "driver/i2c_master.h"
#  include "esp_log.h"

static const char *TAG_I2C = "I2C";

esp_err_t i2c_init(i2c_master_bus_handle_t *bus_handle, i2c_port_t port, int sda_pin, int scl_pin, int i2c_speed) {
  esp_err_t err;

#  if ULP_COMP == 1
i2c_config_t conf = {
      .mode = I2C_MODE_MASTER,
      .sda_io_num = sda_pin,
      .scl_io_num = scl_pin,
      .sda_pullup_en = GPIO_PULLUP_DISABLE,
      .scl_pullup_en = GPIO_PULLUP_DISABLE,
      .master = {.clk_speed = i2c_speed},
      .clk_flags = I2C_SCLK_SRC_FLAG_FOR_NOMAL,
  };
  ESP_LOGI(TAG_I2C, "Init i2c");
  port = I2C_NUM_0;
  err = i2c_param_config(port, &conf);
  if (err != ESP_OK) {
    ESP_LOGE(TAG_I2C, "Failed to configure I2C! Error: %s", esp_err_to_name(err));
    return ESP_ERR_INVALID_STATE;
  }
  err = i2c_driver_install(port, conf.mode, 0, 0, 0);
  if (err != ESP_OK) {
    ESP_LOGE(TAG_I2C, "Failed to install the i2c driver! Error: %s", esp_err_to_name(err));
    return ESP_ERR_INVALID_STATE;
  }
#  elif ULP_COMP == 0
  i2c_master_bus_config_t bus_cfg = {};
  bus_cfg.i2c_port = port;
  bus_cfg.sda_io_num = sda_pin;
  bus_cfg.scl_io_num = scl_pin;
  bus_cfg.clk_source = I2C_CLK_SRC_DEFAULT;
  bus_cfg.glitch_ignore_cnt = 7;
  bus_cfg.intr_priority = 0;
  bus_cfg.flags.enable_internal_pullup = 0;
  bus_cfg.flags.allow_pd = 0;

  err = i2c_new_master_bus(&bus_cfg, bus_handle);
  if(err != ESP_OK){
    ESP_LOGE(TAG_I2C, "Failed to install the i2c driver! Error: %s", esp_err_to_name(err));
    return ESP_ERR_INVALID_STATE;
  }
#  endif
 
  return ESP_OK;
}

esp_err_t i2c_write(i2c_master_dev_handle_t dev_handle, uint8_t address, uint8_t const* data,
                    uint8_t len, int32_t ms_dur) {
  return i2c_master_transmit(dev_handle, data, len, ms_dur);
}

esp_err_t i2c_read(i2c_master_dev_handle_t dev_handle, uint8_t address, uint8_t* data, uint8_t len,
                   int32_t ms_dur) {
  return i2c_master_receive(dev_handle, data, len, ms_dur);
}

esp_err_t i2c_write_read(i2c_master_dev_handle_t dev_handle, uint8_t address,
                         uint8_t const* write_data, uint8_t write_len,
                         uint8_t* read_data, uint8_t read_len, int32_t ms_dur) {
  return i2c_master_transmit_receive(dev_handle, write_data, write_len,
                                      read_data, read_len,
                                      ms_dur);
}

void wait_for(int32_t us_dur) {
  // wait min 1 tick
  static TickType_t one_tick_ms = pdTICKS_TO_MS(1);
  uint32_t ms_dur = us_dur / 1000U;
  if (ms_dur < one_tick_ms) {
    ms_dur = (uint32_t)one_tick_ms;
  }
  vTaskDelay(pdMS_TO_TICKS(ms_dur));
}
#endif
