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

if(AE_EXP_PREPARED_WIFI_CACHE_5X20)
  list(APPEND src_list
    "prepared_wifi_cache_5x20_bench.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_KEEP_WIFI_UP_5X20)
  list(APPEND src_list
    "prepared_keep_wifi_up_5x20_bench.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_WIFI_FASTEST)
  list(APPEND src_list
    "prepared_wifi_fastest_path_bench.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_DEEPSLEEP_5X50)
  list(APPEND src_list
    "prepared_deepsleep_5x50_bench.cpp"
    "experiment_early_entry.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_FINAL_D1_5X50)
  list(APPEND src_list
    "prepared_final_d1_5x50_bench.cpp"
    "experiment_early_entry.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_AP_AETHERNETIO_3X10)
  list(APPEND src_list
    "prepared_ap_aethernetio_3x10_bench.cpp"
    "experiment_early_entry.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5)
  list(APPEND src_list
    "prepared_ap_aethernetio_nosleep_5x5_bench.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_FULL_AETHER_AETHERNETIO)
  list(APPEND src_list
    "full_aether_aethernetio_loop.cpp"
  )
elseif(AE_EXP_PREPARED_TX_DONE_DIAG)
  list(APPEND src_list
    "prepared_tx_done_diag_bench.cpp"
    "experiment_early_entry.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_MAC_RETRY_DIAG)
  list(APPEND src_list
    "prepared_mac_retry_diag_bench.cpp"
    "experiment_early_entry.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_BOOT_WIFI_OPT)
  list(APPEND src_list
    "prepared_boot_wifi_opt_bench.cpp"
    "experiment_early_entry.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_BOOT_WIFI_VAL100)
  list(APPEND src_list
    "prepared_boot_wifi_val100_bench.cpp"
    "experiment_early_entry.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_WIFI_BISECT)
  list(APPEND src_list
    "prepared_wifi_single_factor_bisect_bench.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_PREPARED_MESSAGE_E2E)
  list(APPEND src_list
    "prepared_message_e2e_bench.cpp"
    "prepared_send/prepared_send.cpp"
  )
elseif(AE_EXP_WIFI_LIFECYCLE)
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
  CPMAddPackage(URI "https://github.com/aethernetio/aether-client-cpp.git#157aadbec8e7b852d0f89274307ff7cb8103e5f7")

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
set(AE_EXP_PREPARED_MESSAGE_E2E "" CACHE STRING "No-sleep prepared-message E2E bench (set to 1)")
set(AE_EXP_PREPARED_WIFI_CACHE_5X20 "" CACHE STRING "Silent 5x20 prepared Wi-Fi cache bench (set to 1)")
set(AE_EXP_PREPARED_KEEP_WIFI_UP_5X20 "" CACHE STRING "Silent 5x20 keep-Wi-Fi-up prepared bench (set to 1)")
set(AE_EXP_PREPARED_WIFI_BISECT "" CACHE STRING "Silent single-factor prepared Wi-Fi bisect (set to 1)")
set(AE_EXP_PREPARED_WIFI_FASTEST "" CACHE STRING "Silent fastest-path prepared campaign (set to 1)")
set(AE_EXP_PREPARED_DEEPSLEEP_5X50 "" CACHE STRING "Silent deep-sleep 5x50 prepared E2E (set to 1)")
set(AE_EXP_PREPARED_FINAL_D1_5X50 "" CACHE STRING "Final D1 WIFI_STORAGE_RAM deep-sleep 5x50 (set to 1)")
set(AE_EXP_PREPARED_AP_AETHERNETIO_3X10 "" CACHE STRING "aethernetio AP 3x10 1s deep sleep (set to 1)")
set(AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5 "" CACHE STRING "aethernetio no-sleep 5x5 reliability (set to 1)")
set(AE_EXP_FULL_AETHER_AETHERNETIO "" CACHE STRING "Canonical FULL Aether aethernetio baseline (set to 1)")
set(AE_EXP_PREPARED_TX_DONE_DIAG "" CACHE STRING "Silent TX-done callback diagnostic 1x50 (set to 1)")
set(AE_EXP_PREPARED_MAC_RETRY_DIAG "" CACHE STRING "Silent MAC retry-limit diagnostic 7x50 (set to 1)")
set(AE_EXP_PREPARED_BOOT_WIFI_OPT "" CACHE STRING "Silent boot/wifi HOT opt campaign 11x30 (set to 1)")
set(AE_EXP_PREPARED_BOOT_WIFI_VAL100 "" CACHE STRING "Silent VAL100 combined boot/wifi winners (set to 1)")
set(AE_EXP_TX_DIAG_MODE "" CACHE STRING "TX-done diag mode 0=FIRST_ANY 1=FIRST_SUCCESS")
set(AE_EXP_FAST_N "" CACHE STRING "Fastest-path prepared count")
set(AE_EXP_FAST_TEST_ID "" CACHE STRING "Fastest-path test id")
set(AE_EXP_FAST_PRE_MS "" CACHE STRING "Fastest-path pre-send delay ms")
set(AE_EXP_FAST_POST_MS "" CACHE STRING "Fastest-path post-send delay ms")
set(AE_EXP_FAST_USE_BSSID "" CACHE STRING "Fastest-path cached BSSID (0/1)")
set(AE_EXP_FAST_FAST_SCAN "" CACHE STRING "Fastest-path WIFI_FAST_SCAN (0/1)")
set(AE_EXP_FAST_AUTH "" CACHE STRING "Fastest-path auth 0=WPA3 1=H2E 2=WPA2")
set(AE_EXP_FAST_RETRY "" CACHE STRING "Fastest-path association retry max")
set(AE_EXP_FAST_POST_MODE "" CACHE STRING "Fastest-path post 0=delay 1=cb 2=cb+10 3=cb+25")
set(AE_EXP_FAST_AMPDU_TX_OFF "" CACHE STRING "Fastest-path AMPDU TX off (0/1)")
set(AE_EXP_FAST_STORAGE_RAM "" CACHE STRING "Fastest-path WIFI_STORAGE_RAM (0/1)")
set(AE_EXP_BISECT_CONSOLE "" CACHE STRING "Bisect USB stage markers (set to 1)")
set(AE_EXP_BISECT_SMOKE "" CACHE STRING "Bisect smoke B1/2 prepared (set to 1)")
set(BENCH_CLIENT_ID "" CACHE STRING "Bench SelectClient id (default prepared_message_bench_v1)")
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
ae_exp_define_if_set(AE_EXP_PREPARED_MESSAGE_E2E)
ae_exp_define_if_set(AE_EXP_PREPARED_WIFI_CACHE_5X20)
ae_exp_define_if_set(AE_EXP_PREPARED_KEEP_WIFI_UP_5X20)
ae_exp_define_if_set(AE_EXP_PREPARED_WIFI_BISECT)
ae_exp_define_if_set(AE_EXP_PREPARED_WIFI_FASTEST)
ae_exp_define_if_set(AE_EXP_PREPARED_DEEPSLEEP_5X50)
ae_exp_define_if_set(AE_EXP_PREPARED_FINAL_D1_5X50)
ae_exp_define_if_set(AE_EXP_PREPARED_AP_AETHERNETIO_3X10)
ae_exp_define_if_set(AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5)
ae_exp_define_if_set(AE_EXP_FULL_AETHER_AETHERNETIO)
ae_exp_define_if_set(AE_EXP_PREPARED_TX_DONE_DIAG)
ae_exp_define_if_set(AE_EXP_PREPARED_MAC_RETRY_DIAG)
ae_exp_define_if_set(AE_EXP_PREPARED_BOOT_WIFI_OPT)
ae_exp_define_if_set(AE_EXP_PREPARED_BOOT_WIFI_VAL100)
ae_exp_define_if_set(AE_EXP_TX_DIAG_MODE)
ae_exp_define_if_set(AE_EXP_FAST_N)
ae_exp_define_if_set(AE_EXP_FAST_TEST_ID)
ae_exp_define_if_set(AE_EXP_FAST_PRE_MS)
ae_exp_define_if_set(AE_EXP_FAST_POST_MS)
ae_exp_define_if_set(AE_EXP_FAST_USE_BSSID)
ae_exp_define_if_set(AE_EXP_FAST_FAST_SCAN)
ae_exp_define_if_set(AE_EXP_FAST_AUTH)
ae_exp_define_if_set(AE_EXP_FAST_RETRY)
ae_exp_define_if_set(AE_EXP_FAST_POST_MODE)
ae_exp_define_if_set(AE_EXP_FAST_AMPDU_TX_OFF)
ae_exp_define_if_set(AE_EXP_FAST_STORAGE_RAM)
ae_exp_define_if_set(AE_EXP_BISECT_CONSOLE)
ae_exp_define_if_set(AE_EXP_BISECT_SMOKE)
  if(AE_EXP_PREPARED_WIFI_CACHE_5X20 STREQUAL "1" OR
   AE_EXP_PREPARED_KEEP_WIFI_UP_5X20 STREQUAL "1" OR
   AE_EXP_PREPARED_WIFI_FASTEST STREQUAL "1" OR
   AE_EXP_PREPARED_DEEPSLEEP_5X50 STREQUAL "1" OR
   AE_EXP_PREPARED_FINAL_D1_5X50 STREQUAL "1" OR
   AE_EXP_PREPARED_AP_AETHERNETIO_3X10 STREQUAL "1" OR
   AE_EXP_PREPARED_AP_AETHERNETIO_NOSLEEP_5X5 STREQUAL "1" OR
   AE_EXP_PREPARED_TX_DONE_DIAG STREQUAL "1" OR
   AE_EXP_PREPARED_MAC_RETRY_DIAG STREQUAL "1" OR
   AE_EXP_PREPARED_BOOT_WIFI_OPT STREQUAL "1" OR
   AE_EXP_PREPARED_BOOT_WIFI_VAL100 STREQUAL "1" OR
   (AE_EXP_PREPARED_WIFI_BISECT STREQUAL "1" AND
    NOT AE_EXP_BISECT_CONSOLE STREQUAL "1"))
  target_compile_definitions(aether PUBLIC "AE_EXP_SILENT=1")
  target_compile_definitions(${TARGET_NAME} PRIVATE "AE_EXP_SILENT=1")
endif()
if(NOT "${BENCH_CLIENT_ID}" STREQUAL "")
  target_compile_definitions(${TARGET_NAME} PRIVATE
    "BENCH_CLIENT_ID=\"${BENCH_CLIENT_ID}\"")
endif()

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
