param(
  [string]$Port = 'COM7',
  [string]$ServiceUid = '3d284a4f-ebb4-451e-a2c5-aecb0d647a45',
  [int]$CaptureSec = 600
)
$ErrorActionPreference = 'Stop'
$env:Path = 'C:\Program Files\Git\cmd;C:\Espressif\python_env\idf6.0_py3.11_env\Scripts;' + $env:Path
$env:IDF_PATH = 'C:\Espressif\frameworks\esp-idf-v6.0.2'
. "$env:IDF_PATH\export.ps1" | Out-Null

$proj = 'C:\Users\nickc\Projects\temperature-sensor-prepared'
$aether = 'C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0'
$build = 'build-esp32c6-save-bench-smoke'
$exp = Join-Path $proj 'experiments\wifi_lifecycle'
New-Item -ItemType Directory -Force -Path $exp | Out-Null
$log = Join-Path $exp 'v30_ignore_bssid_capture.log'
$rebootLog = Join-Path $exp 'v30_ignore_bssid_reboot3.log'

function Stop-EspMonitors {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'esp_idf_monitor' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
}

Set-Location $proj
Stop-EspMonitors

# Baseline driver + ignore BSSID only. Do NOT erase flash / SPIFFS.
$cmakeArgs = @(
  '-B', $build,
  '-D', 'WIFI_SSID=chirkov',
  '-D', 'WIFI_PASSWORD=kcdjepWz51',
  '-D', "SERVICE_UID=$ServiceUid",
  '-D', "CPM_aether-client-cpp_SOURCE=$aether",
  '-D', 'AE_EXP_WIFI_LIFECYCLE=1',
  '-D', 'AE_EXP_WIFI_LIFECYCLE_VARIANT=30',
  '-D', 'AE_EXP_WIFI_LIFECYCLE_CYCLES=10',
  '-D', 'AE_EXP_SKIP_DTOR_SAVE=1',
  '-D', 'AE_EXP_WIFI_COOLDOWN_MS=0',
  '-D', 'AE_EXP_WIFI_CANONICAL=0',
  '-D', 'AE_EXP_WIFI_IGNORE_BSSID=1',
  '-D', 'AE_EXP_WIFI_FEAT_AMPDU_OFF=0',
  '-D', 'AE_EXP_WIFI_FEAT_SCAN_THRESHOLD=0',
  '-D', 'AE_EXP_WIFI_FEAT_CONNECT_RETRY10=0',
  '-D', 'AE_EXP_WIFI_FEAT_FAIL_DISCONNECT=0',
  '-D', 'AE_EXP_WIFI_FEAT_BSSID_CACHE=0',
  '-D', 'AE_EXP_WIFI_FEAT_LEGACY_DEINIT=0',
  '-D', 'AE_EXP_WIFI_FEAT_LEGACY_HANDLERS=0',
  '-D', 'AE_EXP_FULL_CYCLES=',
  '-D', 'AE_EXP_BENCH_STAGE=',
  '-D', 'AE_EXP_SAVE_BENCH_N=',
  '-D', 'AE_EXP_LEGACY_MAP_SYNC='
)

Write-Output 'BUILD variant=30 baseline+IGNORE_BSSID (no flash erase)'
idf.py @cmakeArgs build flash -p $Port
if ($LASTEXITCODE -ne 0) { throw 'build/flash failed' }

Remove-Item $log -Force -ErrorAction SilentlyContinue
$elf = Join-Path $proj "$build\temperature_sensor.elf"
$mon = Start-Process -FilePath python -ArgumentList @('-u','-m','esp_idf_monitor','--port',$Port,'--baud','115200',$elf) -WorkingDirectory (Join-Path $proj $build) -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru -NoNewWindow
Start-Sleep -Seconds 2
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
python -m esptool --chip esp32c6 -p $Port run 2>&1 | Out-Null
$ErrorActionPreference = $prev

$deadline = (Get-Date).AddSeconds($CaptureSec)
while ((Get-Date) -lt $deadline) {
  if ((Test-Path $log) -and (Select-String -Path $log -Pattern 'WIFI_SUMMARY' -Quiet)) { break }
  Start-Sleep -Seconds 5
}
Stop-Process -Id $mon.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Output '--- 10-cycle result ---'
Select-String -Path $log -Pattern '^(cycle=|raw=|min=|median=|max=|completed=|WIFI_SUMMARY)' | ForEach-Object { $_.Line }

# Reboot check: 3 cycles, SPIFFS preserved (no erase)
Write-Output '--- reboot + 3 cycles (SPIFFS kept) ---'
Remove-Item $rebootLog -Force -ErrorAction SilentlyContinue
$mon2 = Start-Process -FilePath python -ArgumentList @('-u','-m','esp_idf_monitor','--port',$Port,'--baud','115200',$elf) -WorkingDirectory (Join-Path $proj $build) -RedirectStandardOutput $rebootLog -RedirectStandardError "$rebootLog.err" -PassThru -NoNewWindow
Start-Sleep -Seconds 2
$ErrorActionPreference = 'Continue'
python -m esptool --chip esp32c6 -p $Port run 2>&1 | Out-Null
$ErrorActionPreference = $prev

$deadline2 = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $deadline2) {
  if (Test-Path $rebootLog) {
    $n = @(Select-String -Path $rebootLog -Pattern '^cycle=\d+ init_to_release_ms=').Count
    if ($n -ge 3) { break }
  }
  Start-Sleep -Seconds 3
}
Stop-Process -Id $mon2.Id -Force -ErrorAction SilentlyContinue

Write-Output '--- reboot 3-cycle lines ---'
Select-String -Path $rebootLog -Pattern '^cycle=\d+ init_to_release_ms=' | Select-Object -First 3 | ForEach-Object { $_.Line }
Write-Output "LOG10=$log"
Write-Output "LOG3=$rebootLog"
