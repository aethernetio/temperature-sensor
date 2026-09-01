# Restart the product adaptive probe campaign if it dies before both APs finish.
# Never restarts while a build, flash, receiver or the runner itself is active.
$ErrorActionPreference = "Continue"
$Root = "C:\Users\nickc\Projects\temperature-sensor-prepared"
$cp = Join-Path $Root "experiments\product_probe_checkpoint.json"
$log = Join-Path $Root "experiments\product_adaptive_probe_watchdog.log"
$progress = Join-Path $Root "experiments\adaptive_probe_progress.log"
$ps1 = Join-Path $Root "experiments\run_product_adaptive_probe.ps1"
$ApCount = 2

function Write-Log($msg) {
  $line = "$(Get-Date -Format 'HH:mm:ss') $msg"
  Add-Content -Path $log -Value $line -Encoding utf8
  Write-Host $line
}

function Get-ApIndex {
  if (-not (Test-Path $cp)) { return 0 }
  try {
    $j = Get-Content $cp -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -ne $j.fatal -and "$($j.fatal)" -ne "") {
      $j.PSObject.Properties.Remove("fatal")
      ($j | ConvertTo-Json -Depth 20) | Set-Content -Path $cp -Encoding utf8
      Write-Log "cleared fatal from checkpoint"
    }
    return [int]$j.ap_index
  } catch { return 0 }
}

function CampaignBusy {
  $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "run_product_adaptive_probe\.py" -or
    $_.CommandLine -match "probe-receiver\.exe" -or
    $_.CommandLine -match "esptool" -or
    ($_.Name -match "ninja|cmake" -and $_.CommandLine -match "build-esp32c6-adaptive-probe")
  }
  if ($null -ne $procs) { return $true }
  if (Test-Path $progress) {
    $age = (Get-Date) - (Get-Item $progress).LastWriteTime
    if ($age.TotalMinutes -lt 8) { return $true }
  }
  return $false
}

Write-Log "watchdog start"
while ($true) {
  $index = Get-ApIndex
  if ($index -ge $ApCount) {
    Write-Log "campaign complete ap_index=$index"
    break
  }
  if (CampaignBusy) {
    Write-Log "ok busy ap_index=$index"
  } else {
    Write-Log "idle at ap_index=$index - restart campaign"
    $ppk = Join-Path $Root "experiments\ppk2-venv\Scripts\python.exe"
    $ppkScript = Join-Path $Root "experiments\ppk2_power.py"
    if (Test-Path $ppk) { & $ppk $ppkScript --voltage-mv 3000 | Out-Null }
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
      "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ps1
    ) -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep -Seconds 120
  }
  Start-Sleep -Seconds 120
}
Write-Log "watchdog exit"
