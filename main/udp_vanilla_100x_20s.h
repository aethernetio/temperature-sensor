/*
 * Copyright 2026 Aethernet Inc.
 *
 * Vanilla ESP-IDF Wi-Fi UDP 100× / 20 s deep-sleep baseline.
 */

#ifndef TEMP_SENSOR_UDP_VANILLA_100X_20S_H_
#define TEMP_SENSOR_UDP_VANILLA_100X_20S_H_

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_UDP_VANILLA_100X_20S)
extern "C" void RunUdpVanilla100x20s();
#endif

#endif  // TEMP_SENSOR_UDP_VANILLA_100X_20S_H_
