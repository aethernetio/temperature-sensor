"""Host test for sleep deadlines and rejected sleep; requires a C++ compiler."""
import argparse
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cc", default="g++")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name in ("aether/all.h", "user_config.h", "esp_timer.h", "hal/uart_types.h", "soc/gpio_num.h"):
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        (folder / "esp_log.h").write_text(
            '#define ESP_LOGI(...) ((void)0)\n#define ESP_LOGE(...) ((void)0)\n'
        )
        (folder / "esp_sleep.h").write_text(
            '#include <cstdint>\nusing esp_err_t = int;\n#define ESP_OK 0\n'
            'int esp_sleep_enable_timer_wakeup(std::uint64_t);\n'
            'int esp_deep_sleep_try_to_start();\n'
        )
        (folder / "esp_system.h").write_text('[[noreturn]] void esp_restart();\n')
        (folder / "sensors").mkdir()
        (folder / "sensors/sensors.h").write_text(
            '#define BOARD_HAS_STCC4 1\n#define BOARD_HAS_PWR_ON 1\n'
            '#define BOARD_HAS_ULP 0\nvoid PowerOffSensors();\n'
        )
        source = folder / "test.cpp"
        source.write_text('#include "' + (root / 'main/sleeping/esp_main_sleep.cpp').as_posix() + '"\n' + r'''
#include <cassert>
struct Restart {};
struct Sleeping {};
static std::uint64_t timer;
static int timer_error, sleep_error, sleep_calls;
static bool powered_off;
void PowerOffSensors() { powered_off = true; }
int esp_sleep_enable_timer_wakeup(std::uint64_t us) { timer = us; return timer_error; }
int esp_deep_sleep_try_to_start() {
  assert(powered_off);
  ++sleep_calls;
  if (sleep_error) return sleep_error;
  throw Sleeping{}; // Successful deep sleep never returns.
}
[[noreturn]] void esp_restart() { throw Restart{}; }
int main() {
  using namespace std::chrono;
  for (auto offset : {seconds{-300}, seconds{0}, seconds{30}}) {
    powered_off = false;
    auto deadline = system_clock::now() + offset;
    try { DeepSleep(deadline, deadline, 0); assert(false); } catch (Sleeping&) {}
    if (offset <= seconds{0}) assert(timer == 1000000);
    else assert(timer > 1000000 && timer <= 30000000);
  }
  sleep_error = 0x103;
  try { DeepSleep(system_clock::now(), {}, 0); assert(false); } catch (Restart&) {}
  assert(timer == 1000000);
  timer_error = 0x102;
  int before = sleep_calls;
  try { DeepSleep(system_clock::now(), {}, 0); assert(false); } catch (Restart&) {}
  assert(sleep_calls == before);
}
''')
        exe = folder / "test.exe"
        if Path(args.cc).name.lower() in ("cl", "cl.exe"):
            command = [args.cc, "/nologo", "/EHsc", "/std:c++17", "/DESP_MAIN_SLEEP=1",
                       "/I" + str(folder), "/I" + str(root / "main"),
                       str(source), "/Fe:" + str(exe)]
        else:
            command = [args.cc, "-std=c++17", "-DESP_MAIN_SLEEP=1",
                        "-I" + str(folder), "-I" + str(root / "main"),
                        str(source), "-o", str(exe)]
        subprocess.run(command, cwd=folder, check=True)
        subprocess.run([str(exe)], check=True)
    print("PASS: overdue/future sleep deadlines, sleep rejection, timer failure")


if __name__ == "__main__":
    main()
