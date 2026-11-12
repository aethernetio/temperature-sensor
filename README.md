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

This project registers no desktop CTest tests.

Desktop compile checks do not validate sensor hardware, sleep/wake behavior,
Wi-Fi lifecycle, or cloud delivery. The ESP-IDF main application retains its
Wi-Fi lifecycle SDK revision `157aadbec8e7b852d0f89274307ff7cb8103e5f7` and pins
its compatible libbcrypt revision `8aa32ad94ebe06b76853b0767c910c9fbf7ccef4` before
the SDK is configured. The consumer's telemetry declaration overrides that SDK's
historical Tele pin without replacing its Wi-Fi/prepared-send/sleep implementation.
The C-only ULP coprocessor neither includes nor links the network SDK and does not
fetch it.

Firmware CI at revision `d632134492779a5b26def3378ad0e50842327861` compiled all
three configured boards (M5Stack, AetherC6 and NanoC6):
[successful build run](https://github.com/aethernetio/temperature-sensor/actions/runs/43302614390).
That historical check predates the telemetry-main update. No physical sensor,
sleep/wake, Wi-Fi or cloud-delivery test was run.

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
