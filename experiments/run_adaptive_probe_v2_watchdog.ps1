# Autoresume V2 campaign if the orchestrator exits before step 10.
# Do NOT restart while receiver/esptool/ninja are active or progress is fresh.
$ErrorActionPreference = "Continue"
$Root = "C:\Users\nickc\Projects\temperature-sensor-prepared"
$cp = Join-Path $Root "experiments\adaptive_probe_checkpoint.json"
$log = Join-Path $Root "experiments\adaptive_probe_v2_watchdog.log"
$progress = Join-Path $Root "experiments\adaptive_probe_progress.log"
$ps1 = Join-Path $Root "experiments\run_adaptive_probe_v2.ps1"

function Write-Log($msg) {
  $line = "$(Get-Date -Format 'HH:mm:ss') $msg"
  Add-Content -Path $log -Value $line -Encoding utf8
  Write-Host $line
}

function Get-StepIndex {
  if (-not (Test-Path $cp)) { return 0 }
  try {
    $j = Get-Content $cp -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -ne $j.fatal -and "$($j.fatal)" -ne "") {
      $j.PSObject.Properties.Remove("fatal")
      ($j | ConvertTo-Json -Depth 20) | Set-Content -Path $cp -Encoding utf8
      Write-Log "cleared fatal from checkpoint"
    }
    return [int]$j.step_index
  } catch { return 0 }
}

function CampaignBusy {
  $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "run_adaptive_probe_v2_campaign\.py" -or
    $_.CommandLine -match "temperature_receiver\.exe" -or
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

Write-Log "watchdog start (conservative)"
while ($true) {
  $step = Get-StepIndex
  if ($step -ge 10) {
    Write-Log "campaign complete step=$step"
    break
  }
  if (CampaignBusy) {
    Write-Log "ok busy step=$step"
  } else {
    Write-Log "idle at step=$step - restart campaign"
    $ppk = Join-Path $Root "experiments\ppk2-venv\Scripts\python.exe"
    $ppkScript = Join-Path $Root "experiments\ppk2_power.py"
    if (Test-Path $ppk) { & $ppk $ppkScript --voltage-mv 3000 | Out-Null }
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
      "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ps1
    ) -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep -Seconds 90
  }
  Start-Sleep -Seconds 90
}
Write-Log "watchdog exit"
