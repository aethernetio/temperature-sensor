# Product adaptive Wi-Fi probe campaign (self-selecting firmware, TCP receiver).
# Pass -BuildOnly to compile the firmware and the receiver without hardware.
param(
  [switch]$BuildOnly,
  [ValidateSet("chirkov", "aethernetio")]
  [string]$Ap,
  [switch]$ReportOnly
)

$ErrorActionPreference = "Continue"
$Root = "C:\Users\nickc\Projects\temperature-sensor-prepared"
$py = "C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe"
$env:IDF_PATH = "C:\Espressif\frameworks\esp-idf-v6.0.2"
$env:IDF_TOOLS_PATH = "C:\Espressif"
$env:ESP_ROM_ELF_DIR = "C:\Espressif\tools\esp-rom-elfs\20241011"
$env:IDF_PYTHON = $py
$env:PYTHON = $py
# Prefer ESP tools ahead of any host binutils; strip msys64 so ULP objcopy stays RISC-V.
$espPref = @(
  "C:\Espressif\tools\cmake\3.30.2\bin",
  "C:\Espressif\tools\ninja\1.12.1",
  "C:\Espressif\tools\riscv32-esp-elf\esp-15.2.0_20251204\riscv32-esp-elf\bin",
  "C:\Espressif\tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64",
  "C:\Espressif\python_env\idf6.0_py3.11_env\Scripts",
  "C:\Program Files\Git\cmd"
) -join ";"
# The host build of the receiver needs the untouched PATH; the ESP build must
# not see msys64 or the ULP objcopy resolves to the wrong architecture.
$env:AE_HOST_PATH = $env:Path
$filtered = ($env:Path -split ";" | Where-Object {
  $_ -and ($_ -notmatch '(?i)msys64') -and ($_ -notmatch '(?i)WindowsApps') -and ($_ -notmatch '(?i)\\Git\\mingw64\\bin')
}) -join ";"
$env:Path = $espPref + ";" + $filtered
Set-Location $Root

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$log = Join-Path $Root "experiments\product_adaptive_probe.log"

# Not $args: that is an automatic variable and splatting it is a footgun.
$pyArgs = @("-u", "experiments/run_product_adaptive_probe.py")
if ($BuildOnly) { $pyArgs += "--build-only" }
if ($ReportOnly) { $pyArgs += "--report-only" }
if ($Ap) { $pyArgs += @("--ap", $Ap) }

& $py @pyArgs 2>&1 | ForEach-Object {
  $line = "$_"
  Add-Content -Path $log -Value $line -Encoding utf8
  Write-Host $line
}
exit $LASTEXITCODE
