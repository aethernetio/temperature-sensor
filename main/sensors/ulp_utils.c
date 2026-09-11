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

#if defined ESP_PLATFORM && ULP_COMP == 1

#  include "utils.h"

#  include "ulp_lp_core.h"
#  include "ulp_lp_core_i2c.h"
#  include "ulp_lp_core_utils.h"

esp_err_t i2c_init(int /*bus_handle*/, i2c_port_t i2c_handle_port, int sda_pin, int scl_pin, int i2c_speed) { return ESP_OK; }

esp_err_t i2c_write(i2c_port_t i2c_handle_port, uint8_t address, uint8_t const* data,
                    uint8_t len, int32_t ms_dur) {
  return lp_core_i2c_master_write_to_device(i2c_handle_port, address, data, len, ms_dur);
}

esp_err_t i2c_read(i2c_port_t i2c_handle_port, uint8_t address, uint8_t* data, uint8_t len,
                   int32_t ms_dur) {
  return lp_core_i2c_master_read_from_device(i2c_handle_port, address, data, len, ms_dur);
}

esp_err_t i2c_write_read(i2c_port_t i2c_handle_port, uint8_t address,
                         uint8_t const* write_data, uint8_t write_len,
                         uint8_t* read_data, uint8_t read_len, int32_t ms_dur) {
  return lp_core_i2c_master_write_read_device(
      i2c_handle_port, address, write_data, write_len, read_data, read_len, ms_dur);
}

void wait_for(int32_t us_dur) { ulp_lp_core_delay_us(us_dur); }

#endif
