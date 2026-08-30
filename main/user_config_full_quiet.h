/*
 * Copyright 2024 Aethernet Inc.
 *
 * Quiet FULL aethernetio baseline: no Æther tele console (UDP/P2P only).
 */

#ifndef USER_CONFIG_H_
#define USER_CONFIG_H_

#if ESP_PLATFORM
#  include "sdkconfig.h"
#endif
#include "aether/config_consts.h"

#define AE_CRYPTO_ASYNC AE_HYDRO_CRYPTO_PK
#define AE_CRYPTO_SYNC AE_HYDRO_CRYPTO_SK
#define AE_SIGNATURE AE_HYDRO_SIGNATURE
#define AE_KDF AE_HYDRO_KDF

#if AE_DISTILLATION || AE_FILTRATION
#  define AE_SUPPORT_REGISTRATION 1
#  define AE_SUPPORT_CLOUD_DNS 1
#else
#  define AE_SUPPORT_REGISTRATION 0
#  define AE_SUPPORT_CLOUD_DNS 0
#endif

#define AE_SUPPORT_SPIFS_FS 1

// Quiet: no tele console on ESP for UDP-delivery-only observation.
#define AE_TELE_ENABLED 0
#define AE_TELE_LOG_CONSOLE 0
#define AE_TELE_LOG_TO_STATISTICS 0
#define AE_TELE_DEBUG_MODULES 0

#define AE_STATISTICS_MAX_SIZE 1024

#if defined ESP_PLATFORM
#  define BOARD_AETHER_ESP32_C6 0
#  define BOARD_FIRE_BEETLE2_С6 1
#  define BOARD_NANO_ESP32_C6 2
#  define BOARD_WROVER_ESP32 3
#  define BOARD_M5STACK_ATOM_LITE 4

#  ifndef BOARD
#    define BOARD BOARD_AETHER_ESP32_C6
#  endif

#  if BOARD == BOARD_AETHER_ESP32_C6
#    include "boards/aether_esp32_c6.h"
#  elif BOARD == BOARD_M5STACK_ATOM_LITE
#    include "boards/m5stack_atom_lite.h"
#  elif BOARD == BOARD_NANO_ESP32_C6
#    include "boards/nano_esp32_c6.h"
#  endif
#endif

#if (BOARD_HAS_BME688 != 1) && (BOARD_HAS_SHT45 != 1) && \
    (BOARD_HAS_SHTC3 != 1) && (BOARD_HAS_STCC4 != 1)
#  if (BOARD_HAS_ULP != 1) || defined IS_ULP_COCPU
#    define TEMP_SENSOR_RANDOM 1
#  endif
#endif

#if BOARD_HAS_ULP == 1
#  define ULP_SLEEP 1
#elif defined ESP_PLATFORM
#  define ESP_MAIN_SLEEP 1
#else
#  define THREAD_SLEEP 1
#endif

#if defined IS_ULP_COCPU
#  define ULP_COMP 1
#else
#  define ULP_COMP 0
#endif

#endif  // USER_CONFIG_H_
