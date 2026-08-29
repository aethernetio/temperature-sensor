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
set(AE_EXP_FAST_DISABLE_WPA3 "" CACHE STRING
    "Benchmark-only: disable CONFIG_ESP_WIFI_ENABLE_WPA3_SAE (set to 1)")
set(AE_EXP_BISECT_CONSOLE "" CACHE STRING
    "Bisect USB stage markers / console (set to 1; disables silent)")
set(AE_EXP_BISECT_SMOKE "" CACHE STRING
    "Bisect smoke: B1 only, 2 prepared (set to 1)")
if(AE_EXP_BISECT_CONSOLE STREQUAL "1" AND
   AE_EXP_PREPARED_WIFI_BISECT STREQUAL "1")
  list(APPEND SDKCONFIG_DEFAULTS
       "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.bench")
elseif(AE_EXP_PREPARED_DEEPSLEEP_5X50 STREQUAL "1")
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
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)


set(AE_FILTRATION On CACHE BOOL "Set filtration mode by default" FORCE)

# add user provided config wich will be included as regular .h file
# Add a user-provided config file, which will be included as a regular .h file
set(USER_CONFIG "user_config.h" CACHE PATH "" FORCE)
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
