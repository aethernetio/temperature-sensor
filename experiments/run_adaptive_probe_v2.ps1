# V2 two-router adaptive probe campaign (TCP receiver).
$ErrorActionPreference = "Stop"
$Root = "C:\Users\nickc\Projects\temperature-sensor-prepared"
$py = "C:\Espressif\python_env\idf6.0_py3.11_env\Scripts\python.exe"
$env:IDF_PATH = "C:\Espressif\frameworks\esp-idf-v6.0.2"
$env:IDF_TOOLS_PATH = "C:\Espressif"
$env:ESP_ROM_ELF_DIR = "C:\Espressif\tools\esp-rom-elfs\20241011"
Set-Location $Root
. "$env:IDF_PATH\export.ps1"
# Do NOT prepend msys64 here — it poisons ESP-IDF cmake/objcopy discovery.
# Receiver launch adds msys64 DLLs via receiver_env(launch=True) only.
$log = Join-Path $Root "experiments\adaptive_probe_v2_campaign.log"
& $py -u experiments/run_adaptive_probe_v2_campaign.py 2>&1 | Tee-Object -FilePath $log
exit $LASTEXITCODE
