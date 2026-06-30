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
  "controller.cpp"
)

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

  CPMAddPackage(URI "https://github.com/aethernetio/aether-client-cpp.git#main")

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
        idf::esp_http_server
        idf::esp_event
        idf::esp_timer
        idf::esp_netif
        idf::esp_hw_support)

    set(TARGET_NAME "${COMPONENT_LIB}")
    include(../cmake/CPM.cmake)
    CPMAddPackage(URI "https://github.com/aethernetio/aether-client-cpp.git#main")

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
