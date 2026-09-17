/*
 * Copyright 2026 Aethernet Inc.
 *
 * Cold Wi-Fi connect stage diagnostic 100× / 2 s sleep.
 */

#ifndef TEMP_SENSOR_WIFI_CONNECT_STAGE_100X_H_
#define TEMP_SENSOR_WIFI_CONNECT_STAGE_100X_H_

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_WIFI_CONNECT_STAGE_100X)
extern "C" void RunWifiConnectStage100x();
#endif

#endif  // TEMP_SENSOR_WIFI_CONNECT_STAGE_100X_H_
