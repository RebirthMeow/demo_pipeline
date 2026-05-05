$ErrorActionPreference = 'Stop'

function Get-Timestamp { Get-Date -Format "HH:mm:ss" }

Write-Host "[( $(Get-Timestamp) )] Starting Stage 1+2: regen_pipeline.ps1"
$start = Get-Date
.\regen_pipeline.ps1 -SkipBackupCheck -SkipWipe
$end = Get-Date
$duration = $end - $start
Write-Host "[( $(Get-Timestamp) )] Stage 1+2 Finished. Duration: $($duration.TotalSeconds)s"

# Check for the output JSON
$jsonPath = "python\predict\benchmark_batch_frags.json"
if (Test-Path $jsonPath) {
    $frags = (Get-Content $jsonPath | ConvertFrom-Json).Count
    Write-Host "Found $frags frags in benchmark_batch_frags.json"
} else {
    Write-Host "Warning: benchmark_batch_frags.json not found!"
}
