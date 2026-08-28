param(
  [int]$Variant = 0,
  [int]$Canonical = 0,
  [int]$CooldownMs = 0,
  [int]$FeatAmpdu = 0,
  [int]$FeatThreshold = 0,
  [int]$FeatRetry10 = 0,
  [int]$FeatFailDisc = 0,
  [int]$FeatBssid = 0,
  [int]$FeatLegacyDeinit = 0,
  [int]$FeatLegacyHandlers = 0,
  [string]$Port = 'COM7',
  [string]$ServiceUid = '3d284a4f-ebb4-451e-a2c5-aecb0d647a45',
  [int]$CaptureSec = 900
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
$log = Join-Path $exp "v${Variant}_capture.log"
$tsv = Join-Path $exp 'wifi_lifecycle_results.tsv'

function Stop-EspMonitors {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'esp_idf_monitor' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
}

Set-Location $proj
$args = @(
  '-B', $build,
  '-D', 'WIFI_SSID=chirkov',
  '-D', 'WIFI_PASSWORD=kcdjepWz51',
  '-D', "SERVICE_UID=$ServiceUid",
  '-D', "CPM_aether-client-cpp_SOURCE=$aether",
  '-D', 'AE_EXP_WIFI_LIFECYCLE=1',
  '-D', "AE_EXP_WIFI_LIFECYCLE_VARIANT=$Variant",
  '-D', 'AE_EXP_WIFI_LIFECYCLE_CYCLES=10',
  '-D', 'AE_EXP_SKIP_DTOR_SAVE=1',
  '-D', "AE_EXP_WIFI_COOLDOWN_MS=$CooldownMs",
  '-D', "AE_EXP_WIFI_CANONICAL=$Canonical",
  '-D', "AE_EXP_WIFI_FEAT_AMPDU_OFF=$FeatAmpdu",
  '-D', "AE_EXP_WIFI_FEAT_SCAN_THRESHOLD=$FeatThreshold",
  '-D', "AE_EXP_WIFI_FEAT_CONNECT_RETRY10=$FeatRetry10",
  '-D', "AE_EXP_WIFI_FEAT_FAIL_DISCONNECT=$FeatFailDisc",
  '-D', "AE_EXP_WIFI_FEAT_BSSID_CACHE=$FeatBssid",
  '-D', "AE_EXP_WIFI_FEAT_LEGACY_DEINIT=$FeatLegacyDeinit",
  '-D', "AE_EXP_WIFI_FEAT_LEGACY_HANDLERS=$FeatLegacyHandlers",
  '-D', 'AE_EXP_FULL_CYCLES=',
  '-D', 'AE_EXP_BENCH_STAGE=',
  '-D', 'AE_EXP_SAVE_BENCH_N=',
  '-D', 'AE_EXP_LEGACY_MAP_SYNC='
)
Write-Output "BUILD variant=$Variant canonical=$Canonical cooldown_ms=$CooldownMs"
Stop-EspMonitors
idf.py @args build flash -p $Port
if ($LASTEXITCODE -ne 0) { throw "build/flash failed" }

Remove-Item $log -Force -ErrorAction SilentlyContinue
$elf = Join-Path $proj "$build\temperature_sensor.elf"
$mon = Start-Process -FilePath python -ArgumentList @('-u','-m','esp_idf_monitor','--port',$Port,'--baud','115200',$elf) -WorkingDirectory (Join-Path $proj $build) -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru -NoNewWindow
Start-Sleep -Seconds 2
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
python -m esptool --chip esp32c6 -p $Port run 2>&1 | Out-Null
$ErrorActionPreference = $prevEap
$deadline = (Get-Date).AddSeconds($CaptureSec)
$summary = $false
while ((Get-Date) -lt $deadline) {
  if (Test-Path $log) {
    if (Select-String -Path $log -Pattern 'WIFI_SUMMARY' -Quiet) { $summary = $true; break }
  }
  Start-Sleep -Seconds 10
}
Stop-Process -Id $mon.Id -Force -ErrorAction SilentlyContinue

$cycles = @()
if (Test-Path $log) {
  $cycles = Select-String -Path $log -Pattern '^WIFI_CYCLE\t' | ForEach-Object { $_.Line }
  $sumLine = (Select-String -Path $log -Pattern '^WIFI_SUMMARY\t' | Select-Object -Last 1).Line
}
Write-Output "SUMMARY=$sumLine"
$cycles | ForEach-Object { Write-Output $_ }

if ($sumLine) {
  if (-not (Test-Path $tsv) -or (Get-Item $tsv).Length -eq 0) {
    "variant`tcompleted`tmin`tmedian`tmax`traw_times`tverdict" | Out-File $tsv -Encoding utf8
  }
  $raw = ($cycles | ForEach-Object { ($_ -split "`t" | Where-Object { $_ -match 'init_to_release_ms=' }) -replace 'init_to_release_ms=','' }) -join ','
  $parts = $sumLine -split "`t"
  $get = { param($k) ($parts | Where-Object { $_ -like "$k=*" }) -replace "$k=",'' }
  $row = "{0}`t{1}`t{2}`t{3}`t{4}`t{5}`t" -f $Variant, (&$get 'completed'), (&$get 'min'), (&$get 'median'), (&$get 'max'), $raw
  Add-Content $tsv $row
}
Write-Output "LOG=$log"
