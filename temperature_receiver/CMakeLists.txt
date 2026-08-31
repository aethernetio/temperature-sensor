# Copyright 2026 Aethernet Inc.
cmake_minimum_required(VERSION 3.16)
project(temperature_receiver LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(USER_CONFIG "${CMAKE_CURRENT_LIST_DIR}/user_config.h" CACHE PATH "Aether user config header")
set(AE_DISTILLATION OFF CACHE BOOL "" FORCE)
set(AE_FILTRATION ON CACHE BOOL "" FORCE)

include("${CMAKE_CURRENT_LIST_DIR}/../cmake/CPM.cmake")
# Pin exact SHA. Override locally with -DCPM_aether-client-cpp_SOURCE=<path>.
  CPMAddPackage(URI "https://github.com/aethernetio/aether-client-cpp.git#157aadbec8e7b852d0f89274307ff7cb8103e5f7")

# MinGW + Parallel STL emits #pragma message noise that breaks -Werror builds.
if(TARGET aether AND CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
  target_compile_options(aether PRIVATE -Wno-error -Wno-cpp)
endif()

add_executable(temperature_receiver main.cpp)
target_include_directories(temperature_receiver PRIVATE
  "${CMAKE_CURRENT_LIST_DIR}/../main")
target_compile_definitions(temperature_receiver PRIVATE
  "USER_CONFIG=\"${USER_CONFIG}\"")
target_link_libraries(temperature_receiver PRIVATE aether)

# Keep aether state under ./state next to the binary cwd.
# FileSystemStdStorage always uses relative "state/" directory.
