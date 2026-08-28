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

list(APPEND src_list
  "main.cpp"
)

if(AE_EXP_WIFI_LIFECYCLE)
  list(APPEND src_list "wifi_lifecycle_bench.cpp")
elseif(AE_EXP_FULL_CYCLES)
  list(APPEND src_list "thermometer_full_cycles.cpp")
else()
  list(APPEND src_list
    "controller.cpp"
    "prepared_send/prepared_send.cpp"
  )
endif()

list(APPEND bme68x_srcs
  "../third-party/BME68x_SensorAPI/bme68x.c"
)

list(APPEND sleeping_src
  "sleeping/esp_main_sleep.cpp"
  "sleeping/thread_sleep.cpp"
  "sleeping/ulp_sleep.cpp"
)

list(APPEND sensors_src
  "sensors/ulp_utils.c"
  "sensors/esp_main_utils.c"
  "sensors/bme688.c"
  "sensors/shtc3.c"
  "sensors/stcc4.c"
  "sensors/sht45.c"
  "sensors/random_val.c"
  "sensors/ulp_saved_val.c"
)

set(SERVICE_UID "" CACHE STRING "Service for collecting data UUID")

if(NOT CM_PLATFORM)
  project("temperature-sensor-app" VERSION "1.0.0" LANGUAGES C CXX)

  include(../cmake/CPM.cmake)

  # Pin exact SHA. Override locally with -DCPM_aether-client-cpp_SOURCE=<path>.
  CPMAddPackage(URI "https://github.com/aethernetio/aether-client-cpp.git#a2ee9ec97af4f83e8507a51b46bf2f03393984c1")

  add_executable(${PROJECT_NAME} ${src_list} ${sleeping_src} ${sensors_src})
  set(TARGET_NAME ${PROJECT_NAME})
  target_include_directories(${TARGET_NAME} PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
  target_include_directories(${TARGET_NAME} PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/..)
  target_link_libraries(${TARGET_NAME} PRIVATE aether)
else()
  if(CM_PLATFORM STREQUAL "ESP32")
    set(WIFI_SSID "" CACHE STRING "WiFi SSID")
    set(WIFI_PASSWORD "" CACHE STRING "WiFi Password")
    set(BOARD "" CACHE STRING "Selected board - see user_config.h")

    #ESP32 CMake
    idf_component_register(SRCS ${src_list} ${bme68x_srcs} ${sleeping_src} ${sensors_src}
      INCLUDE_DIRS ${CMAKE_CURRENT_SOURCE_DIR} "${CMAKE_CURRENT_SOURCE_DIR}/.."
      REQUIRES soc ulp
      PRIV_REQUIRES
        idf::driver
        idf::esp_driver_i2c
        idf::esp_driver_tsens
        idf::esp_driver_gpio
        idf::esp_driver_usb_serial_jtag
        idf::esp_http_server
        idf::esp_event
        idf::esp_timer
        idf::esp_netif
        idf::esp_hw_support)

    set(TARGET_NAME "${COMPONENT_LIB}")
    include(../cmake/CPM.cmake)
    # Pin exact SHA. Override locally with -DCPM_aether-client-cpp_SOURCE=<path>.
    CPMAddPackage(URI "https://github.com/aethernetio/aether-client-cpp.git#157aadbec8e7b852d0f89274307ff7cb8103e5f7")

    target_link_libraries(${TARGET_NAME} PRIVATE aether)

    if (NOT "${WIFI_SSID}" STREQUAL "")
      target_compile_definitions(${TARGET_NAME} PRIVATE "WIFI_SSID=\"${WIFI_SSID}\"")
    endif()
    if (NOT "${WIFI_PASSWORD}" STREQUAL "")
      target_compile_definitions(${TARGET_NAME} PRIVATE "WIFI_PASSWORD=\"${WIFI_PASSWORD}\"")
    endif()
    if (NOT "${BOARD}" STREQUAL "")
      target_compile_definitions(${TARGET_NAME} PRIVATE "BOARD=${BOARD}")
    endif()

    # Define the path to the board configuration file in the build folder
    set(ULP_BOARD_FILE "${CMAKE_BINARY_DIR}/ulp_board_config.cmake")

    # Write the value of the BOARD variable to this file
    if(NOT "${BOARD}" STREQUAL "")
      file(WRITE "${ULP_BOARD_FILE}" "set(BOARD \"${BOARD}\")\n")
    else()
      file(WRITE "${ULP_BOARD_FILE}" "set(BOARD \"\")\n")
    endif()

    # We call the project addition as usual
    ulp_add_project("ulp_main" "${CMAKE_CURRENT_LIST_DIR}/../ulp")

  else()
    #Other platforms
    message(FATAL_ERROR "Platform ${CM_PLATFORM} is not supported")
  endif()
endif()

if (NOT "${SERVICE_UID}" STREQUAL "")
  target_compile_definitions(${TARGET_NAME} PRIVATE "SERVICE_UID=\"${SERVICE_UID}\"")
endif()

# Experiment 2 Save measurement optional flags (do NOT set AE_EXP_DIAG here).
# Apply PUBLIC on aether so library instrumentation sees the same defs.
# Example: -DAE_EXP_SAVE_STATS=1 -DAE_EXP_SKIP_DTOR_SAVE=1
set(AE_EXP_SAVE_STATS "" CACHE STRING "Enable AE_EXP_SAVE_STATS (set to 1)")
set(AE_EXP_SKIP_DTOR_SAVE "" CACHE STRING "Skip AetherApp dtor Save (set to 1)")
set(AE_EXP_LEGACY_MAP_SYNC "" CACHE STRING "Enable AE_EXP_LEGACY_MAP_SYNC (set to 1)")
set(AE_EXP_CALL_COUNT_MODE "" CACHE STRING "Call-count mode 1/2/3 (optional)")
set(AE_EXP_FULL_CYCLES "" CACHE STRING "FULL cycle count (e.g. 30); disables Save bench when set")
set(AE_EXP_BENCH_STAGE "" CACHE STRING "Enable UART stage markers (set to 1)")
set(AE_EXP_SAVE_BENCH_N "" CACHE STRING "Save bench samples per scenario (default 50, smoke=2)")
set(AE_EXP_SAVE_BENCH_CD_CHUNK "" CACHE STRING "C/D chunk size (default 10)")
set(AE_EXP_WIFI_LIFECYCLE "" CACHE STRING "Wi-Fi lifecycle bench harness (set to 1)")
set(AE_EXP_WIFI_LIFECYCLE_VARIANT "" CACHE STRING "Wi-Fi lifecycle variant id (0=baseline)")
set(AE_EXP_WIFI_LIFECYCLE_CYCLES "" CACHE STRING "Wi-Fi lifecycle cycles (default 10)")
set(AE_EXP_WIFI_COOLDOWN_MS "" CACHE STRING "Cooldown between cycles, outside timer (ms)")
set(AE_EXP_WIFI_CANONICAL "" CACHE STRING "Canonical ESP-IDF Wi-Fi driver (set to 1)")
set(AE_EXP_WIFI_FEAT_AMPDU_OFF "" CACHE STRING "Canonical+bisect: AMPDU off")
set(AE_EXP_WIFI_FEAT_SCAN_THRESHOLD "" CACHE STRING "Canonical+bisect: scan threshold")
set(AE_EXP_WIFI_FEAT_CONNECT_RETRY10 "" CACHE STRING "Canonical+bisect: 10 connect retries")
set(AE_EXP_WIFI_FEAT_FAIL_DISCONNECT "" CACHE STRING "Canonical+bisect: fail disconnect path")
set(AE_EXP_WIFI_FEAT_LEGACY_DEINIT "" CACHE STRING "Canonical+bisect: baseline Deinit")
set(AE_EXP_WIFI_FEAT_LEGACY_HANDLERS "" CACHE STRING "Canonical+bisect: non-instance handlers")

function(ae_exp_define_if_set flag_name)
  if(NOT "${${flag_name}}" STREQUAL "")
    target_compile_definitions(aether PUBLIC "${flag_name}=${${flag_name}}")
    target_compile_definitions(${TARGET_NAME} PRIVATE "${flag_name}=${${flag_name}}")
  endif()
endfunction()

ae_exp_define_if_set(AE_EXP_SAVE_STATS)
ae_exp_define_if_set(AE_EXP_SKIP_DTOR_SAVE)
ae_exp_define_if_set(AE_EXP_LEGACY_MAP_SYNC)
ae_exp_define_if_set(AE_EXP_CALL_COUNT_MODE)
ae_exp_define_if_set(AE_EXP_FULL_CYCLES)
ae_exp_define_if_set(AE_EXP_BENCH_STAGE)
ae_exp_define_if_set(AE_EXP_SAVE_BENCH_N)
ae_exp_define_if_set(AE_EXP_SAVE_BENCH_CD_CHUNK)
ae_exp_define_if_set(AE_EXP_WIFI_CANONICAL)
ae_exp_define_if_set(AE_EXP_WIFI_FEAT_AMPDU_OFF)
ae_exp_define_if_set(AE_EXP_WIFI_FEAT_SCAN_THRESHOLD)
ae_exp_define_if_set(AE_EXP_WIFI_FEAT_CONNECT_RETRY10)
ae_exp_define_if_set(AE_EXP_WIFI_FEAT_FAIL_DISCONNECT)
ae_exp_define_if_set(AE_EXP_WIFI_FEAT_LEGACY_DEINIT)
ae_exp_define_if_set(AE_EXP_WIFI_FEAT_LEGACY_HANDLERS)
ae_exp_define_if_set(AE_EXP_WIFI_LIFECYCLE)
ae_exp_define_if_set(AE_EXP_WIFI_LIFECYCLE_VARIANT)
ae_exp_define_if_set(AE_EXP_WIFI_LIFECYCLE_CYCLES)
ae_exp_define_if_set(AE_EXP_WIFI_COOLDOWN_MS)

# Continuous prepared-send E2E test defaults (override via -D...).
if(NOT DEFINED AETHER_PREPARED_NONCE_RESERVE)
  set(AETHER_PREPARED_NONCE_RESERVE 10 CACHE STRING "Prepared nonce reserve count")
endif()
if(NOT DEFINED AETHER_PREPARED_HOT_SLEEP_SECONDS)
  set(AETHER_PREPARED_HOT_SLEEP_SECONDS 10 CACHE STRING "Deep sleep seconds after send")
endif()
if(NOT DEFINED AETHER_PREPARED_POST_SEND_HOLD_MS)
  set(AETHER_PREPARED_POST_SEND_HOLD_MS 300 CACHE STRING "Post-send TX hold ms on prepared path")
endif()
if(NOT DEFINED AETHER_PREPARED_WIFI_PS)
  set(AETHER_PREPARED_WIFI_PS 0 CACHE STRING "Prepared WiFi PS mode (0=NONE,1=MIN,2=MAX)")
endif()
if(NOT DEFINED AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS)
  set(AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS 15000 CACHE STRING "Prepared WiFi connect timeout ms")
endif()
if(NOT DEFINED AETHER_PREPARED_HOT_WIFI_MAX_RETRY)
  set(AETHER_PREPARED_HOT_WIFI_MAX_RETRY 10 CACHE STRING "Prepared WiFi max association retries")
endif()
target_compile_definitions(${TARGET_NAME} PRIVATE
  "AETHER_PREPARED_NONCE_RESERVE=${AETHER_PREPARED_NONCE_RESERVE}"
  "AETHER_PREPARED_HOT_SLEEP_SECONDS=${AETHER_PREPARED_HOT_SLEEP_SECONDS}"
  "AETHER_PREPARED_POST_SEND_HOLD_MS=${AETHER_PREPARED_POST_SEND_HOLD_MS}"
  "AETHER_PREPARED_WIFI_PS=${AETHER_PREPARED_WIFI_PS}"
  "AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS=${AETHER_PREPARED_HOT_WIFI_TIMEOUT_MS}"
  "AETHER_PREPARED_HOT_WIFI_MAX_RETRY=${AETHER_PREPARED_HOT_WIFI_MAX_RETRY}")
