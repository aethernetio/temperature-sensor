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

The desktop target compiled and linked with MSVC 19.50 x64 Release against
published SDK `a7ec61a5db9af36cf6dcc3dee95ef489a09e470b` on 2026-11-12, using
a fresh CPM cache and no local source overrides. This project registers no
desktop CTest tests.

Desktop compile checks do not validate sensor hardware, sleep/wake behavior,
Wi-Fi lifecycle, or cloud delivery. ESP-IDF and ULP builds retain their separate
SDK revision until those embedded scenarios are revalidated.

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
