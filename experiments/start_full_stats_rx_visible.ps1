# Visible quiet FULL-baseline receiver (keep open).
$ErrorActionPreference = "Continue"
$Root = "C:\Users\nickc\Projects\temperature-sensor-prepared"
$Exe = Join-Path $Root "temperature_receiver\build-bisect\temperature_receiver.exe"
$Sess = Join-Path $Root "experiments\full_aether_stats_rx_session"
$UidFile = Join-Path $Root "experiments\full_aether_stats_receiver_uid.txt"

$env:Path = "C:\msys64\ucrt64\bin;" + $env:Path
$env:AE_RECEIVER_SESSION_DIR = $Sess
$env:AE_DS_BENCH_TAG = "full_aether_stats"

if (Test-Path $Sess) { Remove-Item -Recurse -Force $Sess }
New-Item -ItemType Directory -Force -Path $Sess | Out-Null
if (Test-Path $UidFile) { Remove-Item -Force $UidFile }

$Host.UI.RawUI.WindowTitle = "FULL-RX stats (keep open)"
Set-Location $Sess
Write-Host "Starting quiet temperature_receiver..."
& $Exe 2>&1 | ForEach-Object {
  $line = "$_"
  Write-Host $line
  if ($line -match 'RECEIVER_UID=([0-9a-fA-F-]{36})') {
    Set-Content -Path $UidFile -Value $Matches[1] -Encoding ascii -NoNewline
  }
}
