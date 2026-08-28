$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'run_wifi_lifecycle.ps1'

function Get-Feat($run, [string]$key) {
  if ($run.ContainsKey($key)) { return [int]$run[$key] }
  return 0
}

$runs = @(
  @{ Variant = 10; Name = 'canonical+AMPDU_OFF'; FeatAmpdu = 1 },
  @{ Variant = 11; Name = 'canonical+SCAN_THRESHOLD'; FeatThreshold = 1 },
  @{ Variant = 12; Name = 'canonical+CONNECT_RETRY10'; FeatRetry10 = 1 },
  @{ Variant = 13; Name = 'canonical+FAIL_DISCONNECT'; FeatFailDisc = 1 },
  @{ Variant = 14; Name = 'canonical+BSSID_CACHE'; FeatBssid = 1 },
  @{
    Variant = 15
    Name = 'canonical+ALL_FEATS'
    FeatAmpdu = 1
    FeatThreshold = 1
    FeatRetry10 = 1
    FeatFailDisc = 1
    FeatBssid = 1
  }
)

foreach ($run in $runs) {
  Write-Output "=== BISECT $($run.Name) variant=$($run.Variant) ==="
  & $script -Variant $run.Variant -Canonical 1 -CooldownMs 0 `
    -FeatAmpdu (Get-Feat $run 'FeatAmpdu') `
    -FeatThreshold (Get-Feat $run 'FeatThreshold') `
    -FeatRetry10 (Get-Feat $run 'FeatRetry10') `
    -FeatFailDisc (Get-Feat $run 'FeatFailDisc') `
    -FeatBssid (Get-Feat $run 'FeatBssid')
  if ($LASTEXITCODE -ne 0) {
    Write-Output "WARN: variant $($run.Variant) script exit=$LASTEXITCODE (check log)"
  }
}

Write-Output '=== BISECT DONE ==='
Get-Content (Join-Path $PSScriptRoot 'wifi_lifecycle\wifi_lifecycle_results.tsv')
