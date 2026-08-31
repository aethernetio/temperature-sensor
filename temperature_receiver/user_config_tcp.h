/*
 * Copyright 2026 Aethernet Inc.
 *
 * V2 desktop receiver: quiet tele; runtime TCP enforced via Restream().
 */
#ifndef USER_CONFIG_H_
#define USER_CONFIG_H_

#include "aether/config_consts.h"

#define AE_CRYPTO_ASYNC AE_HYDRO_CRYPTO_PK
#define AE_CRYPTO_SYNC AE_HYDRO_CRYPTO_SK
#define AE_SIGNATURE AE_HYDRO_SIGNATURE
#define AE_KDF AE_HYDRO_KDF

#define AE_TELE_ENABLED 0
#define AE_TELE_LOG_CONSOLE 0
#define AE_TELE_LOG_TO_STATISTICS 0
#define AE_TELE_DEBUG_MODULES 0

#endif  // USER_CONFIG_H_
