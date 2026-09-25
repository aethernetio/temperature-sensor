"""Host regression test for the ULP STCC4 driver; run with --cc <host gcc>."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cc", default="gcc")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "esp_err.h").write_text(
            "#pragma once\ntypedef int esp_err_t;\n"
            "#define ESP_OK 0\n#define ESP_FAIL -1\n"
            "#define ESP_ERR_INVALID_CRC 0x109\n"
        )
        (folder / "esp_log.h").write_text("")
        (folder / "ulp_lp_core_i2c.h").write_text(
            "#pragma once\ntypedef int i2c_port_t;\n#define LP_I2C_NUM_0 0\n"
        )
        (folder / "ulp_lp_core_gpio.h").write_text("")
        harness = '#include "' + (root / "main/sensors/stcc4.c").as_posix() + '"\n'
        harness += r'''
#include <assert.h>
#include <string.h>
static uint8_t frame[12];
static int fail_stage;
esp_err_t i2c_init(int bus, int port, int sda, int scl, int speed) { return 0; }
esp_err_t i2c_write(int port, uint8_t addr, const uint8_t *data,
                    uint8_t len, int32_t timeout) {
  assert(addr == 0x64);
  assert(timeout > 0); // A missing sensor must never block indefinitely.
  if (len == 1) { assert(data[0] == 0); return -1; } // Wake NACK is normal.
  assert(len == 2);
  int cmd = (data[0] << 8) | data[1];
  assert(cmd == 0x219d || cmd == 0xec05);
  return stcc4_stage == (unsigned)fail_stage ? 0x107 : 0;
}
esp_err_t i2c_read(int port, uint8_t addr, uint8_t *data,
                   uint8_t len, int32_t timeout) {
  assert(len == sizeof(frame));
  assert(timeout > 0);
  memcpy(data, frame, len);
  return fail_stage == 4 ? 0x107 : 0;
}
void wait_for(int32_t us) {}
static void make_frame(void) {
  const uint16_t values[] = {800, 26214, 29360, 0};
  for (int i = 0; i < 4; ++i) {
    frame[i * 3] = values[i] >> 8;
    frame[i * 3 + 1] = values[i];
    unsigned crc = 0xff;
    for (int j = 0; j < 2; ++j) {
      crc ^= frame[i * 3 + j];
      for (int b = 0; b < 8; ++b)
        crc = ((crc << 1) ^ ((crc & 0x80) ? 0x31 : 0)) & 0xff;
    }
    frame[i * 3 + 2] = crc;
  }
}
int main(void) {
  int16_t t = -123;
  uint32_t h = 123, co2 = 123;
  make_frame();
  ReadSensors(&t, &h, 0, &co2, 0);
  assert(stcc4_error == 0 && stcc4_stage == 0);
  assert(t == 2500 && h >= 4999 && h <= 5000 && co2 == 800);
  // A bad CRC in any word must leave ALL outputs untouched, including CO2.
  for (int i = 0; i < 4; ++i) {
    make_frame(); frame[i * 3 + 2] ^= 1;
    t = -123; h = co2 = 123;
    ReadSensors(&t, &h, 0, &co2, 0);
    assert(stcc4_error == ESP_ERR_INVALID_CRC && stcc4_stage == 5);
    assert(t == -123 && h == 123 && co2 == 123);
  }
  make_frame();
  for (fail_stage = 2; fail_stage <= 4; ++fail_stage) {
    ReadSensors(&t, &h, 0, &co2, 0);
    assert(stcc4_error == 0x107 && stcc4_stage == (unsigned)fail_stage);
    assert(t == -123 && h == 123 && co2 == 123);
  }
  fail_stage = 0;
  ReadSensors(&t, 0, 0, 0, 0);
  assert(t == 2500 && stcc4_error == 0);
}
'''
        source = folder / "test.c"
        source.write_text(harness)
        exe = folder / "test.exe"
        defines = ["USER_CONFIG_H_", "ESP_PLATFORM=1", "ULP_COMP=1",
                   "BOARD_HAS_ULP=1", "BOARD_HAS_STCC4=1", "BOARD_HAS_PWR_ON=0",
                   "SENSOR_SDA_PIN=6", "SENSOR_SCL_PIN=7"]
        subprocess.run([args.cc, "-std=c11", "-I" + str(folder),
                        "-I" + str(root / "main"),
                        *["-D" + d for d in defines], str(source), "-o", str(exe)],
                       check=True)
        subprocess.run([str(exe)], check=True)
    print("PASS: ULP STCC4 conversion, all CRC words, I2C errors, recovery, optional outputs")


if __name__ == "__main__":
    main()
