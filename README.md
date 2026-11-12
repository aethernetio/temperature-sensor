# Æthernet ESP32 Temperature Sensor

Reference firmware for sending environmental sensor data from an ESP32 through Æthernet. The same codebase can be built as a desktop CMake project for development or as ESP-IDF firmware for supported ESP32 boards.

[Æthernet C++ SDK](https://github.com/aethernetio/aether-client-cpp) · [Documentation](https://aethernet.io/documentation)

## Supported build targets

- desktop CMake build;
- ESP-IDF;
- PlatformIO with ESP-IDF;
- ESP32-C6 DevKitC-1;
- ESP32-S3 DevKitM-1;
- ESP-WROVER-KIT.

## Clone

The project uses Git submodules:

```bash
git clone --recurse-submodules https://github.com/aethernetio/temperature-sensor.git
cd temperature-sensor
```

For an existing clone:

```bash
git submodule update --init --recursive
```

## Desktop build

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

The desktop SDK revision is pinned in `main/CMakeLists.txt`. For local SDK
integration work, configure with
`-DCPM_aether-client-cpp_SOURCE=/path/to/aether-client-cpp`.

Both desktop and ESP-IDF builds declare `aether-tele` from its `main` branch in
`cmake/telemetry.cmake` before the SDK is configured. Numeric and misc dependencies
are pinned to the revisions used by telemetry, so the embedded SDK cannot select
an older codec first. Clean configurations therefore follow telemetry `main`;
an existing CPM cache may require its normal update/reconfiguration flow.

The desktop executable compiled and linked with MSVC 19.50 x64 Release on
2026-11-12 at code revision `069577d3778096bc652331784df6cd1f06d1256c`, using
remote SDK `59153adf1fb8baba088b28b50051fb961e8d6eaf`, Tele `main` resolved to
`65eda03928030b8cc04432e2f98ba4e3242b6f27` and Objects
`80fbb4c33bebd660c9bdf0687311cee30de74f39`. No local dependency source overrides
were used.

This project registers no desktop CTest tests. Its two Python host harnesses run
without ESP hardware or ESP-IDF; they compile and execute small programs using
stubbed platform functions:

```bash
python tests/test_esp_main_sleep.py --cc g++
python tests/test_stcc4_ulp.py --cc gcc
```

Both passed on code revision `069577d3778096bc652331784df6cd1f06d1256c` on
2026-11-12 using MinGW UCRT GCC/G++. They cover sleep deadlines, rejected sleep
and timer errors, plus STCC4 conversions, all CRC words, I2C errors, recovery and
optional output parameters. See [the test guide](tests/readme.md).

Desktop compile checks do not validate sensor hardware, sleep/wake behavior,
Wi-Fi lifecycle, or cloud delivery. The ESP-IDF main application retains its
Wi-Fi lifecycle SDK revision `157aadbec8e7b852d0f89274307ff7cb8103e5f7` and pins
its compatible libbcrypt revision `8aa32ad94ebe06b76853b0767c910c9fbf7ccef4` before
the SDK is configured. The consumer's telemetry declaration overrides that SDK's
historical Tele pin without replacing its Wi-Fi/prepared-send/sleep implementation.
The C-only ULP coprocessor neither includes nor links the network SDK and does not
fetch it.

Firmware CI at code revision `069577d3778096bc652331784df6cd1f06d1256c` compiled all
three configured boards (M5Stack, AetherC6 and NanoC6):
[successful build run](https://github.com/aethernetio/temperature-sensor/actions/runs/43325254997).
All three configurations resolved `aether-tele` from `main` and numeric revision
`9d79bd79188420533bab07c437d617aedfaf6b1a`. No physical sensor, sleep/wake, Wi-Fi or
cloud-delivery test was run.

## ESP-IDF

Make sure `IDF_PATH` is configured, then replace the board and Wi-Fi placeholders:

```bash
idf.py -C . -B build set-target esp32c6
idf.py -C . -B build build \
  -DWIFI_SSID="your-ssid" \
  -DWIFI_PASSWORD="your-password"
idf.py -C . -B build flash monitor
```

Do not commit real Wi-Fi credentials.

## PlatformIO

Choose one of the environments defined in `platformio.ini`:

```bash
pio run -e esp32-c6-devkitc-1 -t upload
pio device monitor
```

Before building, replace the placeholder Wi-Fi values in a local configuration. Avoid committing the modified credentials.

## What this project demonstrates

- integrating the Æthernet C++ client into ESP-IDF;
- configuring filtered release builds for embedded targets;
- collecting sensor data and delivering it through the Æthernet network;
- sharing a codebase between desktop development and ESP32 firmware.

## Troubleshooting

Include the board, ESP-IDF or PlatformIO version, sensor model, build command, and serial log when opening an issue.

## License

Apache License 2.0. See [LICENSE](LICENSE).
