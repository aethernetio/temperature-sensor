param(
  [string]$Port = 'COM7',
  [string]$ServiceUid = '',
  [string]$BenchClientId = 'prepared_message_bench_v6',
  [int]$CaptureSec = 900
)
$ErrorActionPreference = 'Stop'
$env:Path = 'C:\Program Files\Git\cmd;C:\msys64\ucrt64\bin;C:\Espressif\python_env\idf6.0_py3.11_env\Scripts;' + $env:Path
$env:IDF_PATH = 'C:\Espressif\frameworks\esp-idf-v6.0.2'
. "$env:IDF_PATH\export.ps1" | Out-Null

$proj = 'C:\Users\nickc\Projects\temperature-sensor-prepared'
$aether = 'C:/Users/nickc/Projects/aether-client-cpp-prepared-packet-v0'
$buildEsp = 'build-esp32c6-save-bench-smoke'
$rxRoot = Join-Path $proj 'experiments\prepared_message_receiver'
$rxBuild = Join-Path $rxRoot 'build-mingw'
$rxSession = Join-Path $proj 'experiments\prepared_message_rx_session'
$exp = Join-Path $proj 'experiments'
$espLog = Join-Path $exp 'prepared_message_e2e_esp.log'
$rxLog = Join-Path $exp 'prepared_message_e2e_receiver.log'

function Stop-EspMonitors {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'esp_idf_monitor|idf_monitor|serial' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Get-Process prepared_message_receiver -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
}

New-Item -ItemType Directory -Force -Path $exp, $rxSession | Out-Null
Set-Location $proj
Stop-EspMonitors

Write-Output 'BUILD desktop prepared_message_receiver'
New-Item -ItemType Directory -Force -Path $rxBuild | Out-Null
cmake -S $rxRoot -B $rxBuild -G Ninja `
  -DCMAKE_BUILD_TYPE=Release `
  "-DCPM_aether-client-cpp_SOURCE=$aether"
if ($LASTEXITCODE -ne 0) { throw 'receiver cmake failed' }
cmake --build $rxBuild --parallel
if ($LASTEXITCODE -ne 0) { throw 'receiver build failed' }

$env:AE_RECEIVER_SESSION_DIR = $rxSession
$rxExe = Join-Path $proj 'temperature_receiver\build-prepared-e2e\temperature_receiver.exe'
Remove-Item $rxLog -ErrorAction SilentlyContinue
$rxProc = Start-Process -FilePath $rxExe -RedirectStandardOutput $rxLog `
  -RedirectStandardError ($rxLog + '.err') -PassThru -NoNewWindow
Write-Output "receiver pid=$($rxProc.Id)"

# Wait for RECEIVER_UID (allow cloud registration on desktop)
$uid = $ServiceUid
$deadline = (Get-Date).AddMinutes(10)
while ([string]::IsNullOrWhiteSpace($uid) -and (Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 2
  if (Test-Path $rxLog) {
    $m = Select-String -Path $rxLog -Pattern 'RECEIVER_UID=([0-9a-fA-F\-]+)' |
      Select-Object -Last 1
    if ($m) { $uid = $m.Matches[0].Groups[1].Value }
  }
}
if ([string]::IsNullOrWhiteSpace($uid)) {
  throw 'receiver UID not ready'
}
Set-Content -Path (Join-Path $exp 'prepared_message_receiver_uid.txt') -Value $uid
Write-Output "SERVICE_UID=$uid"

Write-Output 'BUILD ESP prepared-message E2E (no flash erase)'
$cmakeArgs = @(
  '-B', $buildEsp,
  '-D', 'WIFI_SSID=chirkov',
  '-D', 'WIFI_PASSWORD=kcdjepWz51',
  '-D', "SERVICE_UID=$uid",
  '-D', "BENCH_CLIENT_ID=$BenchClientId",
  '-D', "CPM_aether-client-cpp_SOURCE=$aether",
  '-D', 'AE_EXP_PREPARED_MESSAGE_E2E=1',
  '-D', 'AE_EXP_SKIP_DTOR_SAVE=1',
  '-D', 'AE_EXP_WIFI_LIFECYCLE=',
  '-D', 'AE_EXP_FULL_CYCLES=',
  '-D', 'AETHER_PREPARED_NONCE_RESERVE=10'
)
idf.py @cmakeArgs reconfigure
if ($LASTEXITCODE -ne 0) { throw 'esp reconfigure failed' }
idf.py -B $buildEsp build
if ($LASTEXITCODE -ne 0) { throw 'esp build failed' }
idf.py -B $buildEsp -p $Port flash
if ($LASTEXITCODE -ne 0) { throw 'esp flash failed' }

Remove-Item $espLog -ErrorAction SilentlyContinue
$mon = Start-Process -FilePath 'python' -ArgumentList @(
  "$env:IDF_PATH\tools\idf_monitor.py", '-p', $Port, '-b', '115200',
  '--print_filter', '*:I'
) -RedirectStandardOutput $espLog -RedirectStandardError ($espLog + '.err') `
  -PassThru -NoNewWindow

Write-Output "monitor pid=$($mon.Id); capture ${CaptureSec}s"
$done = $false
$end = (Get-Date).AddSeconds($CaptureSec)
while ((Get-Date) -lt $end) {
  Start-Sleep -Seconds 5
  if (Test-Path $espLog) {
    if (Select-String -Path $espLog -Pattern 'PREPARED_E2E_DONE' -Quiet) {
      $done = $true
      break
    }
  }
}

Stop-EspMonitors
Write-Output "done=$done"
Write-Output "esp_log=$espLog"
Write-Output "rx_log=$rxLog"
