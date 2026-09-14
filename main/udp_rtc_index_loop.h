/*
 * Copyright 2026 Aethernet Inc.
 *
 * Wi-Fi + UDP index + deep-sleep loop (RTC-backed counter).
 */

#ifndef TEMP_SENSOR_UDP_RTC_INDEX_LOOP_H_
#define TEMP_SENSOR_UDP_RTC_INDEX_LOOP_H_

#if defined(ESP_PLATFORM) && defined(AETHER_DIAG_UDP_RTC_INDEX)
extern "C" void RunUdpRtcIndexLoop();
#endif

#endif  // TEMP_SENSOR_UDP_RTC_INDEX_LOOP_H_
