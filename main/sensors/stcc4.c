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

#if BOARD_HAS_STCC4 == 1
#  if ((ULP_COMP == 1) && (BOARD_HAS_ULP == 1)) || ((ULP_COMP == 0) && (BOARD_HAS_ULP == 0))
#  pragma message("STTC4 is enabled")

#  include <stdint.h>
#  include <stdbool.h>

#  if ULP_COMP == 1
#    include "ulp_lp_core_gpio.h"
#  elif ULP_COMP == 0
#    include "driver/gpio.h"
#    include "esp_log.h"
#    include "esp_rom_sys.h"
#  endif

#  include "sensors/sensors.h"
#  include "sensors/utils.h"

// Constants for STCC4
#  define STCC4_SLAVE_ADDR 0x64  // Typical address for Sensirion sensors
#  define CRC8_POLYNOMIAL 0x31
// Commands (16-bit, transmitted MSB first)
#  define STCC4_CMD_START_CONTINUOUS 0x218B // Command for continuous measurement
#  define STCC4_CMD_STOP_CONTINUOUS  0x3F86 // Stop for continuous measurement
#  define STCC4_CMD_MEASURE_SINGLE_SHOT 0x219D  // Command for single shot measurement
#  define STCC4_CMD_READ_DATA 0xEC05  // Command to read data
#  define STCC4_CMD_SLEEP 0x3650      // Command to enter sleep

// Constants
#  if ULP_COMP == 1
// LP driver takes CPU cycles (100 ms at 16 MHz); HP takes milliseconds.
#    define STCC4_I2C_TIMEOUT 1600000
#  else
#    define STCC4_I2C_TIMEOUT 100
#  endif
#  define I2C_BUS_SPEED 100000 // 100 KHz

// I2C Buffers
static uint8_t data_wr[2];
static uint8_t data_rd[12];
static bool initialized = false;
// Exported through the ULP linker script for main-CPU diagnostics.
volatile uint32_t stcc4_error;
volatile uint32_t stcc4_stage;
#  if ULP_COMP == 0
static const char* TAG_STCC4 = "STCC4";
static i2c_master_bus_handle_t bus_handle;
static i2c_master_dev_handle_t dev_handle_stcc4;

// Preserve the working startup sequence before handing the pins to hardware I2C.
// Never drive a high level: both lines are open drain with external pull-ups.
static bool startup_scl_high(void) {
  gpio_set_level(SENSOR_SCL_PIN, 1);
  for (unsigned i = 0; i < 1000; ++i) {
    if (gpio_get_level(SENSOR_SCL_PIN)) {
      esp_rom_delay_us(10);
      return true;
    }
    esp_rom_delay_us(1);
  }
  return false;
}

static int startup_write_byte(uint8_t value) {
  for (unsigned i = 0; i < 8; ++i) {
    gpio_set_level(SENSOR_SCL_PIN, 0);
    gpio_set_level(SENSOR_SDA_PIN, (value & 0x80) != 0);
    esp_rom_delay_us(10);
    if (!startup_scl_high()) return -1;
    value <<= 1;
  }
  gpio_set_level(SENSOR_SCL_PIN, 0);
  gpio_set_level(SENSOR_SDA_PIN, 1);
  esp_rom_delay_us(10);
  if (!startup_scl_high()) return -1;
  int ack = gpio_get_level(SENSOR_SDA_PIN) == 0;
  gpio_set_level(SENSOR_SCL_PIN, 0);
  return ack;
}

static void prepare_gpio_bus(void) {
  gpio_config_t cfg = {
    .pin_bit_mask = (1ULL << SENSOR_SDA_PIN) | (1ULL << SENSOR_SCL_PIN),
    .mode = GPIO_MODE_INPUT_OUTPUT_OD,
    .pull_up_en = GPIO_PULLUP_DISABLE,
    .pull_down_en = GPIO_PULLDOWN_DISABLE,
    .intr_type = GPIO_INTR_DISABLE,
  };
  gpio_set_level(SENSOR_SDA_PIN, 1);
  gpio_set_level(SENSOR_SCL_PIN, 1);
  if (gpio_config(&cfg) != ESP_OK) return;
  // First transaction also supplies the documented wake-up byte; its NACK
  // is allowed. The second transaction tests just the address after waking.
  for (unsigned attempt = 0; attempt < 2; ++attempt) {
    int ack = -1;
    if (startup_scl_high() && gpio_get_level(SENSOR_SDA_PIN)) {
      gpio_set_level(SENSOR_SDA_PIN, 0); // START
      esp_rom_delay_us(10);
      ack = startup_write_byte(STCC4_SLAVE_ADDR << 1);
      if (attempt == 0 && ack == 1) (void)startup_write_byte(0x00);
    }
    gpio_set_level(SENSOR_SCL_PIN, 0);
    gpio_set_level(SENSOR_SDA_PIN, 0);
    esp_rom_delay_us(10);
    (void)startup_scl_high();
    gpio_set_level(SENSOR_SDA_PIN, 1); // STOP
    esp_rom_delay_us(10);
    wait_for(5000);
  }
  gpio_reset_pin(SENSOR_SDA_PIN);
  gpio_reset_pin(SENSOR_SCL_PIN);
}

#  endif


#if ULP_COMP == 1
#  define STCC4_I2C_NUM_0 LP_I2C_NUM_0
#  define I2C_BUS_HANDLE_S 0
#  define I2C_HANDLE_PORT_S STCC4_I2C_NUM_0
#elif ULP_COMP == 0
#  define STCC4_I2C_NUM_0 I2C_NUM_0
#  define I2C_BUS_HANDLE_S &bus_handle
#  define I2C_HANDLE_PORT_S dev_handle_stcc4
#endif

bool check_crc(const uint8_t *data, uint8_t crc);

// Helper function to send a 16-bit command
static esp_err_t send_command_16bit(uint16_t cmd, uint8_t slave_addr) {
  data_wr[0] = (cmd >> 8) & 0xFF;  // High byte
  data_wr[1] = cmd & 0xFF;         // Low byte

  esp_err_t err = i2c_write(I2C_HANDLE_PORT_S, slave_addr, data_wr,
                            sizeof(data_wr), STCC4_I2C_TIMEOUT);

  if (err != ESP_OK) {
    // Bail and try again
    return err;
  }

  return ESP_OK;
}

static void exit_sleep_mode(void) {
  // STCC4 wakes on the single byte 0x00. The sensor may NACK this byte while
  // waking up, so the return code must intentionally be ignored.
  data_wr[0] = 0x00;
  (void)i2c_write(I2C_HANDLE_PORT_S, STCC4_SLAVE_ADDR, data_wr, 1,
                  STCC4_I2C_TIMEOUT);
  wait_for(5000);  // 5 ms
}

bool Init() {
  // 1. power ON
#if BOARD_HAS_PWR_ON == 1
#  if ULP_COMP == 1
  ulp_lp_core_gpio_init(LP_PWR_ON_GPIO);
  ulp_lp_core_gpio_output_enable(LP_PWR_ON_GPIO);
  ulp_lp_core_gpio_set_level(LP_PWR_ON_GPIO, 1);
#  elif ULP_COMP == 0
  gpio_hold_dis((gpio_num_t)PWR_ON_GPIO);
  gpio_set_direction((gpio_num_t)PWR_ON_GPIO, GPIO_MODE_OUTPUT);
  gpio_set_level((gpio_num_t)PWR_ON_GPIO, 1);
#  endif
#endif

  // Let the switched sensor rail settle before preparing the GPIO bus.
#if ULP_COMP == 0
  wait_for(125000);
  prepare_gpio_bus();
#endif

  // 2. Install I2C driver
  if (i2c_init(I2C_BUS_HANDLE_S, STCC4_I2C_NUM_0, SENSOR_SDA_PIN, SENSOR_SCL_PIN, I2C_BUS_SPEED) != ESP_OK) {
    return false;
  }

#if ULP_COMP == 0
  // Configuration of a specific device on the bus
  i2c_device_config_t dev_cfg_stcc4 = {};
  dev_cfg_stcc4.dev_addr_length = I2C_ADDR_BIT_LEN_7;
  dev_cfg_stcc4.device_address = STCC4_SLAVE_ADDR;
  dev_cfg_stcc4.scl_speed_hz = I2C_BUS_SPEED;
  esp_err_t err = i2c_master_bus_add_device(bus_handle, &dev_cfg_stcc4, &dev_handle_stcc4);
  if (err != ESP_OK) {
    ESP_LOGE(TAG_STCC4, "Failed to add I2C device: %s", esp_err_to_name(err));
    i2c_del_master_bus(bus_handle);
    bus_handle = NULL;
    return false;
  }
#endif

  // After power-up STCC4 is already in idle mode. Do not put it to sleep here:
  // waking it requires a separate one-byte 0x00 command. Send that command as
  // well so a warm MCU restart can recover a sensor left asleep by old firmware.
  wait_for(125000);  // 125 ms
  exit_sleep_mode();

  return true;
}

#if ULP_COMP == 0 && BOARD_HAS_PWR_ON == 1
void PowerOffSensors(void) {
  // No more measurements are scheduled in this boot after sleep preparation.
  if (dev_handle_stcc4 != NULL) {
    ESP_ERROR_CHECK(i2c_master_bus_rm_device(dev_handle_stcc4));
    dev_handle_stcc4 = NULL;
  }
  if (bus_handle != NULL) {
    ESP_ERROR_CHECK(i2c_del_master_bus(bus_handle));
    bus_handle = NULL;
  }
  // The external pull-ups are on the switched rail. Release both lines so
  // they cannot back-power an unpowered sensor through its I2C pins.
  ESP_ERROR_CHECK(gpio_set_direction(SENSOR_SDA_PIN, GPIO_MODE_INPUT));
  ESP_ERROR_CHECK(gpio_set_pull_mode(SENSOR_SDA_PIN, GPIO_FLOATING));
  ESP_ERROR_CHECK(gpio_set_direction(SENSOR_SCL_PIN, GPIO_MODE_INPUT));
  ESP_ERROR_CHECK(gpio_set_pull_mode(SENSOR_SCL_PIN, GPIO_FLOATING));
  ESP_ERROR_CHECK(gpio_set_direction((gpio_num_t)PWR_ON_GPIO, GPIO_MODE_OUTPUT));
  ESP_ERROR_CHECK(gpio_set_level((gpio_num_t)PWR_ON_GPIO, 0));
  ESP_ERROR_CHECK(gpio_hold_en((gpio_num_t)PWR_ON_GPIO));
  initialized = false;
}
#endif

void ReadSensors(int16_t* temperature, uint32_t* humidity, uint32_t* pressure,
                 uint32_t* co2, uint32_t* gas_resistance) {
  esp_err_t ret;
  uint16_t tmp;
  stcc4_stage = 1;  // Initialization
  stcc4_error = ESP_FAIL;

  if (!initialized) {
    initialized = Init();
    if (!initialized) {
        return;
    }
  }

  stcc4_stage = 2;  // Start single shot
  ret = send_command_16bit(STCC4_CMD_MEASURE_SINGLE_SHOT, STCC4_SLAVE_ADDR);
#if ULP_COMP == 0
  if (ret == ESP_ERR_INVALID_RESPONSE) {
    // A warm MCU reset does not reset a separately powered sensor. Wake it
    // and distinguish an absent address from a rejected command first.
    exit_sleep_mode();
    esp_err_t reset = i2c_master_bus_reset(bus_handle);
    if (reset != ESP_OK) {
      stcc4_error = reset;
      return;
    }
    esp_err_t probe = i2c_master_probe(bus_handle, STCC4_SLAVE_ADDR,
                                     STCC4_I2C_TIMEOUT);
    if (probe == ESP_OK) {
      ret = send_command_16bit(STCC4_CMD_STOP_CONTINUOUS, STCC4_SLAVE_ADDR);
      if (ret == ESP_OK) {
        wait_for(1200000); // Datasheet: stop completes within 1200 ms.
        ret = send_command_16bit(STCC4_CMD_MEASURE_SINGLE_SHOT, STCC4_SLAVE_ADDR);
      }
    } else {
      ret = probe; // Report the observed timeout instead of the earlier error.
    }
  }
#endif
  stcc4_error = ret;

  if (ret != ESP_OK) {
#if ULP_COMP == 0
    ESP_LOGE(TAG_STCC4, "Failed to start measurement: %s", esp_err_to_name(ret));
#endif
    return;
  }
  wait_for(500000);  // 500 ms, specified single-shot execution time

  // Measurement data is not returned by a bare I2C read. The STCC4 first
  // requires the read_measurement command, followed by a short wait.
  stcc4_stage = 3;  // Request result
  ret = send_command_16bit(STCC4_CMD_READ_DATA, STCC4_SLAVE_ADDR);
  stcc4_error = ret;
  if (ret != ESP_OK) {
#if ULP_COMP == 0
    ESP_LOGE(TAG_STCC4, "Failed to request measurement: %s", esp_err_to_name(ret));
#endif
    return;
  }
  wait_for(1000);  // 1 ms

  // Four 16-bit values (CO2, temperature, humidity and status), each followed
  // by its CRC byte, are transferred as 12 bytes on the wire.
  stcc4_stage = 4;  // Read result
  ret = i2c_read(I2C_HANDLE_PORT_S, STCC4_SLAVE_ADDR, data_rd, sizeof(data_rd),
                 STCC4_I2C_TIMEOUT);
  stcc4_error = ret;

  if (ret != ESP_OK) {
#if ULP_COMP == 0
    ESP_LOGE(TAG_STCC4, "Failed to read measurement: %s", esp_err_to_name(ret));
#endif
    return;
  }
  
  // Validate the whole frame on both CPUs before publishing any values.
  stcc4_stage = 5;  // CRC validation
  for (unsigned i = 0; i < sizeof(data_rd); i += 3) {
    if (!check_crc(&data_rd[i], data_rd[i + 2])) {
      stcc4_error = ESP_ERR_INVALID_CRC;
#if ULP_COMP == 0
      ESP_LOGE(TAG_STCC4, "Measurement CRC mismatch in word %u", i / 3);
#endif
      return;
    }
  }

  // Read CO2
  // Convert to CO2 value (format depends on sensor, typically 16 bits)
  uint16_t co2_ppm = (data_rd[0] << 8) | data_rd[1];

  if (co2) {
    *co2 = (uint32_t)co2_ppm;
  }
  
  // Read Temperature

  // Temperature: T = -45 + 175 * raw_temp / 65535
  tmp = (data_rd[3] << 8) | data_rd[4];
  int32_t temp_x100 = (17500ULL * tmp) / 65535 - 4500;
  if (temperature) {
    *temperature = (int16_t)temp_x100;
  }
  
  // Read Humidity
    
  // Humidity: RH = -6 + 125 * raw_hum / 65535
  tmp = (data_rd[6] << 8) | data_rd[7];
  int32_t hum_x100 = (12500ULL * tmp) / 65535 - 600;
    
  // Humidity limitation within 0..100%
  if (hum_x100 > 10000) hum_x100 = 10000;
  if (hum_x100 < 0) hum_x100 = 0;
    
  if (humidity) {
    *humidity = (uint32_t)hum_x100;
  }

  stcc4_error = ESP_OK;
  stcc4_stage = 0;  // Complete
}

bool check_crc(const uint8_t *data, uint8_t crc) {
    // CRC-8 algorithm for Sensirion (polynomial 0x31, initial value 0xFF)
    uint8_t crc_calc = 0xFF;
    for (int i = 0; i < 2; i++) {
        crc_calc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            if (crc_calc & 0x80) {
                crc_calc = (crc_calc << 1) ^ CRC8_POLYNOMIAL;
            } else {
                crc_calc <<= 1;
            }
        }
    }
    return (crc_calc == crc);
}


#endif
#endif
