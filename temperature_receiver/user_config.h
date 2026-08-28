/*
 * Copyright 2026 Aethernet Inc.
 */
#ifndef USER_CONFIG_H_
#define USER_CONFIG_H_

#include "aether/config_consts.h"

#define AE_CRYPTO_ASYNC AE_HYDRO_CRYPTO_PK
#define AE_CRYPTO_SYNC AE_HYDRO_CRYPTO_SK
#define AE_SIGNATURE AE_HYDRO_SIGNATURE
#define AE_KDF AE_HYDRO_KDF

#define AE_TELE_ENABLED 1
#define AE_TELE_LOG_CONSOLE 1
#if defined NDEBUG
#  define AE_TELE_DEBUG_MODULES 0
#else
#  define AE_TELE_DEBUG_MODULES AE_ALL
#endif

#endif  // USER_CONFIG_H_
