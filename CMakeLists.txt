# Copyright 2026 Aethernet Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

cmake_minimum_required(VERSION 3.16.0)

# make release build by default
if (NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE "Release" CACHE STRING "")
endif()

if (ESP_PLATFORM OR IDF_TARGET)
  include($ENV{IDF_PATH}/tools/cmake/project.cmake)
  set(CM_PLATFORM "ESP32")
endif()

# Bench / silent measurement sdkconfig. Must be set before project().
set(AE_EXP_PREPARED_MESSAGE_E2E "" CACHE STRING
    "No-sleep prepared-message E2E bench (set to 1)")
set(AE_EXP_PREPARED_WIFI_CACHE_5X20 "" CACHE STRING
    "Silent 5x20 prepared Wi-Fi cache bench (set to 1)")
set(AE_EXP_PREPARED_KEEP_WIFI_UP_5X20 "" CACHE STRING
    "Silent 5x20 keep-Wi-Fi-up prepared bench (set to 1)")
set(AE_EXP_PREPARED_WIFI_BISECT "" CACHE STRING
    "Silent single-factor prepared Wi-Fi bisect (set to 1)")
set(AE_EXP_PREPARED_WIFI_FASTEST "" CACHE STRING
    "Silent fastest-path prepared Wi-Fi campaign (set to 1)")
set(AE_EXP_PREPARED_DEEPSLEEP_5X50 "" CACHE STRING
    "Silent deep-sleep 5x50 prepared E2E (set to 1)")
set(AE_EXP_PREPARED_FINAL_D1_5X50 "" CACHE STRING
    "Final D1 WIFI_STORAGE_RAM deep-sleep 5x50 validation (set to 1)")
set(AE_EXP_PREPARED_AP_AETHERNETIO_3X10 "" CACHE STRING
    "D1 prepared path on aethernetio AP, 3x10 1s deep sleep (set to 1)")
set(AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5 "" CACHE STRING
    "No-sleep reliability prepared path on aethernetio 5x5 (set to 1)")
set(AE_EXP_FULL_AETHER_AETHERNETIO "" CACHE STRING
    "Canonical FULL Aether path baseline on aethernetio (set to 1)")
set(AE_EXP_PREPARED_TX_DONE_DIAG "" CACHE STRING
    "Silent TX-done callback diagnostic 1x50 (set to 1)")
set(AE_EXP_PREPARED_MAC_RETRY_DIAG "" CACHE STRING
    "Silent MAC retry-limit diagnostic 7x50 (set to 1)")
set(AE_EXP_PREPARED_BOOT_WIFI_OPT "" CACHE STRING
    "Silent boot/wifi HOT opt campaign 11x30 (set to 1)")
set(AE_EXP_PREPARED_BOOT_WIFI_VAL100 "" CACHE STRING
    "Silent VAL100 combined boot/wifi winners (set to 1)")
set(AE_EXP_TX_DIAG_MODE "" CACHE STRING
    "TX-done diag wait mode: 0=FIRST_ANY 1=FIRST_SUCCESS")
set(AE_EXP_FAST_DISABLE_WPA3 "" CACHE STRING
    "Benchmark-only: disable CONFIG_ESP_WIFI_ENABLE_WPA3_SAE (set to 1)")
set(AE_EXP_BISECT_CONSOLE "" CACHE STRING
    "Bisect USB stage markers / console (set to 1; disables silent)")
set(AE_EXP_BISECT_SMOKE "" CACHE STRING
    "Bisect smoke: B1 only, 2 prepared (set to 1)")
if(AE_EXP_FULL_AETHER_AETHERNETIO STREQUAL "1")
  # Exclusive minimal defaults — do NOT append silent/fastest/wpa2 overlays.
  set(SDKCONFIG_DEFAULTS
      "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.full_aether_baseline")
  # Quiet Æther tele: observe UDP/P2P deliveries only.
  set(USER_CONFIG
      "${CMAKE_CURRENT_LIST_DIR}/main/user_config_full_quiet.h"
      CACHE PATH "" FORCE)
elseif(AE_EXP_BISECT_CONSOLE STREQUAL "1" AND
   AE_EXP_PREPARED_WIFI_BISECT STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.bench")
elseif(AE_EXP_PREPARED_DEEPSLEEP_5X50 STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.deepsleep_5x50")
elseif(AE_EXP_PREPARED_FINAL_D1_5X50 STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.deepsleep_5x50")
elseif(AE_EXP_PREPARED_AP_AETHERNETIO_3X10 STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.deepsleep_5x50")
elseif(AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5 STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only")
elseif(AE_EXP_PREPARED_TX_DONE_DIAG STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.deepsleep_5x50")
elseif(AE_EXP_PREPARED_MAC_RETRY_DIAG STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.deepsleep_5x50")
elseif(AE_EXP_PREPARED_BOOT_WIFI_OPT STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.deepsleep_5x50")
elseif(AE_EXP_PREPARED_BOOT_WIFI_VAL100 STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.deepsleep_5x50")
elseif(AE_EXP_PREPARED_WIFI_FASTEST STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.fastest")
  if(AE_EXP_FAST_DISABLE_WPA3 STREQUAL "1")
    list(APPEND SDKCONFIG_DEFAULTS
         "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.wpa2only")
  endif()
elseif(AE_EXP_PREPARED_WIFI_CACHE_5X20 STREQUAL "1" OR
   AE_EXP_PREPARED_KEEP_WIFI_UP_5X20 STREQUAL "1" OR
   AE_EXP_PREPARED_WIFI_BISECT STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent")
elseif(AE_EXP_PREPARED_MESSAGE_E2E STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.bench")
elseif(AE_EXP_PREPARED_POWER_FACTOR STREQUAL "1")
  # Must include sdkconfig.defaults: setting SDKCONFIG_DEFAULTS replaces IDF's
  # implicit defaults file. Omitting it drops ULP (and other product Kconfig)
  # and leaves ulp_add_project with an empty CMAKE_TOOLCHAIN_FILE.
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.prepared_low_power")
  set(CMAKE_SUPPRESS_REGENERATION ON)
elseif(AE_EXP_PREPARED_FINAL_1MIN_100 STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.silent"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.prepared_low_power")
  set(CMAKE_SUPPRESS_REGENERATION ON)
elseif(AE_EXP_PRODUCT_ADAPTIVE_PROBE STREQUAL "1")
  # Product prepared HOT path uses the measured low-power sdkconfig stack.
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults"
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.prepared_low_power")
endif()

# Default product firmware (no AE_EXP_*): same prepared low-power production path.
if(NOT SDKCONFIG_DEFAULTS)
  set(SDKCONFIG_DEFAULTS
      "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults"
      "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.prepared_low_power")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)


set(AE_FILTRATION On CACHE BOOL "Set filtration mode by default" FORCE)

# add user provided config wich will be included as regular .h file
# Add a user-provided config file, which will be included as a regular .h file
if(NOT AE_EXP_FULL_AETHER_AETHERNETIO STREQUAL "1")
  set(USER_CONFIG "${CMAKE_CURRENT_LIST_DIR}/main/user_config.h" CACHE FILEPATH "" FORCE)
endif()
# ${USER_CONFIG} must be an absolute path or a path to something listed in include directories
include_directories(${CMAKE_CURRENT_LIST_DIR}/main)

if (CM_PLATFORM STREQUAL "ESP32")
  idf_build_set_property(CM_PLATFORM "ESP32")
  add_compile_definitions(CM_ESP32)

  list(APPEND EXTRA_COMPONENT_DIRS "./main")

  project(temperature_sensor LANGUAGES CXX C ASM)
else()
  project(temperature_sensor LANGUAGES CXX C ASM)

  set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
  add_subdirectory("./main" "temperature-sensor")
endif()
