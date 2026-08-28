/*
 * Copyright 2026 Aethernet Inc.
 *
 * USB Serial JTAG output for Wi-Fi lifecycle benchmark (works with CONSOLE_NONE).
 */
#ifndef TEMP_SENSOR_WIFI_LIFECYCLE_OUT_H_
#define TEMP_SENSOR_WIFI_LIFECYCLE_OUT_H_

#if defined(ESP_PLATFORM)
#  include <cstdio>
#  include <cstdarg>

#  include <driver/usb_serial_jtag.h>
#  include <freertos/FreeRTOS.h>

inline void WifiLifecycleEnsureUsbSerial() {
  static bool installed = false;
  if (!installed) {
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_serial_jtag_driver_install(&cfg);
    installed = true;
  }
}

inline void WifiLifecycleWriteRaw(char const* data, size_t len) {
  if (data != nullptr && len > 0) {
    WifiLifecycleEnsureUsbSerial();
    usb_serial_jtag_write_bytes(data, len, portMAX_DELAY);
  }
}

inline void WifiLifecyclePrintf(char const* fmt, ...) {
  char buf[160];
  va_list args;
  va_start(args, fmt);
  int const n = vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  if (n > 0) {
    WifiLifecycleWriteRaw(buf, static_cast<size_t>(n));
  }
}
#else
inline void WifiLifecyclePrintf(char const* /*fmt*/, ...) {}
#endif

#endif  // TEMP_SENSOR_WIFI_LIFECYCLE_OUT_H_
