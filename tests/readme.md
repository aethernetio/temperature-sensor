# Running the STCC4 Test

`test_stcc4_ulp.py` tests the ULP branch of the STCC4 driver on your computer.
No board, sensor, or ESP-IDF installation is required: GPIO, I2C, and delays are stubbed out.

## Requirements

- Python 3. No additional Python packages are required.
- A C compiler for your operating system with C11 support: GCC or Clang.
  The ESP32 compiler (`riscv32-esp-elf-gcc`) is not suitable: the test executable must run on your computer.
- A complete copy of the project: the test uses `main/sensors/stcc4.c` and headers from `main`.

The `--cc` option accepts a compiler name available in `PATH` or a path to the compiler.
It defaults to `gcc`. Specify only the executable, without additional flags.

## Windows (PowerShell)

You need Python and Clang with a linker and C runtime installed
(for example, Visual Studio Build Tools with the C++ components and Windows SDK),
or GCC from MinGW-w64. `cl.exe` and `clang-cl.exe` do not support the arguments used by this script.

If Python and Clang are available in `PATH`, run from the project root:

```powershell
python .\tests\test_stcc4_ulp.py --cc clang
```

If you are already in the `tests` directory:

```powershell
python .\test_stcc4_ulp.py --cc clang
```

If Python Launcher is installed, you can use `py -3` instead of `python`.
For MinGW-w64, replace `--cc clang` with `--cc gcc`.

Example using absolute paths from the current project environment, **from the `tests` directory**:

```powershell
& 'G:\dev\esp32\tools\python\v6.1\venv\Scripts\python.exe' .\test_stcc4_ulp.py --cc 'H:\Program Files\LLVM\bin\clang.exe'
```

On another computer, replace the Python and compiler paths with your own.
The ESP-IDF virtual environment in this example is used only to provide a Python interpreter.

## Linux

You need Python 3 and GCC with the system C headers and libraries.
For example, on Debian/Ubuntu:

```bash
sudo apt update
sudo apt install python3 build-essential
```

From the project root:

```bash
python3 tests/test_stcc4_ulp.py --cc gcc
```

From the `tests` directory:

```bash
python3 ./test_stcc4_ulp.py --cc gcc
```

If Clang is installed, you can use `--cc clang` instead.

## macOS

You need Python 3 and Apple's developer tools with Clang.
To install Command Line Tools if they are not already installed:

```bash
xcode-select --install
```

Check that the tools are available:

```bash
python3 --version
clang --version
```

From the project root:

```bash
python3 tests/test_stcc4_ulp.py --cc clang
```

From the `tests` directory:

```bash
python3 ./test_stcc4_ulp.py --cc clang
```

## Expected Result and Test Coverage

On success, the script exits with code `0` and prints:

```text
PASS: ULP STCC4 conversion, all CRC words, I2C errors, recovery, optional outputs
```

The test checks temperature, humidity, and CO2 conversion, CRC validation for all four words,
unchanged output values on errors, successful reads after an error,
optional output parameters, and positive transaction timeouts.

This test checks driver logic using stubs. It does not test physical I2C signals,
power, the main processor's GPIO startup sequence, actual timeout durations,
or operation after deep sleep.

The test creates and runs a program in a temporary directory, then automatically removes it.
The filename `test.exe` is used on every operating system; on Linux/macOS it is a native executable and does not require Wine.

## Troubleshooting

- **`can't open file .../tests/tests/test_stcc4_ulp.py`**: you are already in the `tests` directory.
  Use `./test_stcc4_ulp.py` without the extra `tests/` prefix.
- **`python` / `python3` not found**: install Python or specify the full path to the interpreter.
- **Compiler not found (`FileNotFoundError`)**: check `PATH` or specify the full path using `--cc`.
  Enclose paths containing spaces in quotes.
- **Linker error or missing `assert.h` / `string.h`**: install the headers,
  C runtime, and linker for your compiler. On Windows, `clang.exe` alone may not be sufficient.
- **`Assertion failed` / `CalledProcessError`**: the test failed. Check the first compiler error
  or failed `assert` above the traceback; no successful `PASS` message will be printed.
- **Execution blocked in the temporary directory**: select a temporary directory that permits execution
  using `TMPDIR` (Linux/macOS) or `TEMP`/`TMP` (Windows) before starting Python.
