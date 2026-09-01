# V2 two-router adaptive probe campaign (TCP receiver).
$ErrorActionPreference = "Continue"
$Root = "C:\Users\nickc\Projects\temperature-sensor-prepared"
$py = "C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe"
$env:IDF_PATH = "C:\Espressif\frameworks\esp-idf-v6.0.2"
$env:IDF_TOOLS_PATH = "C:\Espressif"
$env:ESP_ROM_ELF_DIR = "C:\Espressif\tools\esp-rom-elfs\20241011"
$env:IDF_PYTHON = $py
# Never skip baseline cmake/ninja across AP switches - last image may be the other AP.
$env:AE_V2_SKIP_BASELINE_BUILD = '0'
$env:AE_V2_SKIP_ERASE = '0'
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
$filtered = ($env:Path -split ";" | Where-Object {
  $_ -and ($_ -notmatch '(?i)msys64') -and ($_ -notmatch '(?i)WindowsApps') -and ($_ -notmatch '(?i)\\Git\\mingw64\\bin')
}) -join ";"
$env:Path = $espPref + ";" + $filtered
Set-Location $Root
# Skip export.ps1: activate.py expects a missing idf6.0_py3.12_env; campaign env() is enough.
$log = Join-Path $Root "experiments\adaptive_probe_v2_campaign.log"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
& $py -u experiments/run_adaptive_probe_v2_campaign.py 2>&1 | ForEach-Object {
  $line = "$_"
  Add-Content -Path $log -Value $line -Encoding utf8
  Write-Host $line
}
exit $LASTEXITCODE
