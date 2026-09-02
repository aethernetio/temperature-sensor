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

cmake_minimum_required(VERSION 3.16)

set(ULP_APP_NAME ulp_main)

project(${ULP_APP_NAME})

list(APPEND main_src
  "main.c"
)

list(APPEND sensors_src
  "${CMAKE_CURRENT_LIST_DIR}/../main/sensors/ulp_utils.c"
  "${CMAKE_CURRENT_LIST_DIR}/../main/sensors/random_val.c"
  "${CMAKE_CURRENT_LIST_DIR}/../main/sensors/bme688.c"
  "${CMAKE_CURRENT_LIST_DIR}/../main/sensors/shtc3.c"
  "${CMAKE_CURRENT_LIST_DIR}/../main/sensors/stcc4.c"
  "${CMAKE_CURRENT_LIST_DIR}/../main/sensors/sht45.c"
  "${CMAKE_CURRENT_LIST_DIR}/../third-party/BME68x_SensorAPI/bme68x.c"
)

add_executable(${ULP_APP_NAME} ${main_src} ${sensors_src})

add_compile_definitions(ESP_PLATFORM)
add_compile_definitions(BME68X_DO_NOT_USE_FPU)

# Import the ULP project helper functions
include(IDFULPProject)

# Apply default compile options
ulp_apply_default_options(${ULP_APP_NAME})

# Apply default sources provided by the IDF ULP component
ulp_apply_default_sources(${ULP_APP_NAME})

# Add targets for building the binary, as well as the linkerscript which exports ULP shared variables to the main app
ulp_add_build_binary_targets(${ULP_APP_NAME})

target_include_directories(${ULP_APP_NAME} PUBLIC "${CMAKE_CURRENT_LIST_DIR}/../main")

# Calculate where the build folder of the main project is located
# ExternalProject is usually built inside build/ulp_main-prefix/src/ulp_main-build
# We need to go up to the root of the build folder
set(MAIN_BUILD_DIR "${CMAKE_BINARY_DIR}/../../..")
set(ULP_BOARD_FILE "${MAIN_BUILD_DIR}/ulp_board_config.cmake")

# If the configuration file exists, connect it
if(EXISTS "${ULP_BOARD_FILE}")
    include("${ULP_BOARD_FILE}")
endif()

# Checking if the BOARD variable value appears after including
if(NOT "${BOARD}" STREQUAL "")
    message(STATUS "--------------------------------------------------")
    message(STATUS "ULP Project SUCCESS! Board loaded from file: ${BOARD}")
    message(STATUS "--------------------------------------------------")
    
    # Checking if the BOARD variable has been included
    add_compile_definitions(BOARD=${BOARD})
else()
    message(WARNING "ULP Project: BOARD is not defined!")
endif()
