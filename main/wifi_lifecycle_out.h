/*
 * Copyright 2026 Aethernet Inc.
 *
 * Bench lifecycle markers on stdout (ESP monitor / desktop console).
 */
#ifndef TEMP_SENSOR_WIFI_LIFECYCLE_OUT_H_
#define TEMP_SENSOR_WIFI_LIFECYCLE_OUT_H_

#if defined(ESP_PLATFORM)
#  include <cstdio>
#  include <cstdarg>
#else
#  include <cstdio>
#  include <cstdarg>
#endif

inline void WifiLifecyclePrintf(char const* fmt, ...) {
  va_list args;
  va_start(args, fmt);
  std::vprintf(fmt, args);
  va_end(args);
  std::fflush(stdout);
}

#endif  // TEMP_SENSOR_WIFI_LIFECYCLE_OUT_H_
