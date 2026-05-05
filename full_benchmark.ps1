$ErrorActionPreference = 'Stop'

function Get-Timestamp { Get-Date -Format "HH:mm:ss" }

$stages = @()

# --- Stage 1 & 2 ---
Write-Host "[( $(Get-Timestamp) )] Starting Stage 1+2: regen_pipeline.ps1"
$s1_start = Get-Date
.\regen_pipeline.ps1 -SkipBackupCheck -SkipWipe
$s1_end = Get-Date
$stages += [pscustomobject]@{ Name = "Stage 1+2 (Regen+Scan)"; Sec = ($s1_end - $s1_start).TotalSeconds }
Write-Host "[( $(Get-Timestamp) )] Stage 1+2 Finished."

# --- Stage 3 ---
$jsonPath = "python\predict\benchmark_batch_frags.json"
if (-not (Test-Path $jsonPath)) {
    throw "benchmark_batch_frags.json not found! Stopping."
}
Write-Host "[( $(Get-Timestamp) )] Starting Stage 3: predict_frags_ensemble.py"
$s3_start = Get-Date
& python python\predict\predict_frags_ensemble.py $jsonPath
$s3_end = Get-Date
$stages += [pscustomobject]@{ Name = "Stage 3 (Predict)"; Sec = ($s3_end - $s3_start).TotalSeconds }
Write-Host "[( $(Get-Timestamp) )] Stage 3 Finished."

# --- Stage 4 ---
$csvPath = "python\predict\benchmark_batch_frags.ensemble_predictions.csv"
if (-not (Test-Path $csvPath)) {
    throw "benchmark_batch_frags.ensemble_predictions.csv not found! Stopping."
}
Write-Host "[( $(Get-Timestamp) )] Starting Stage 4: build_jamme_demolist.py"
$s4_start = Get-Date
& python python\predict\build_jamme_demolist.py --csv $csvPath
$s4_end = Get-Date
$stages += [pscustomobject]@{ Name = "Stage 4 (Stage)"; Sec = ($s4_end - $s4_start).TotalSeconds }
Write-Host "[( $(Get-Timestamp) )] Stage 4 Finished."

# --- Stage 5 ---
Write-Host "[( $(Get-Timestamp) )] Starting Stage 5: render_clips.py"
# We'll use the resilient runner. We'll limit it to 5 clips if there are many,
# just to get a per-clip average without taking hours, but the user asked
# for "execution time of this process" so I'll try to run them all if it's
# under a reasonable limit (e.g. 20).
$manifestPath = "a full jka install\game_directory\Jedi Academy\GameData\mme\clip_manifest.json"
$clipCount = 0
if (Test-Path $manifestPath) {
    $clipCount = (Get-Content $manifestPath | ConvertFrom-Json).Count
    Write-Host "Staged $clipCount clips for rendering."
}

$s5_start = Get-Date
# We'll run the bat file. 
Push-Location "a full jka install\game_directory\Jedi Academy\GameData"
try {
    .\render_resilient.bat
} finally {
    Pop-Location
}
$s5_end = Get-Date
$stages += [pscustomobject]@{ Name = "Stage 5 (Render)"; Sec = ($s5_end - $s5_start).TotalSeconds }
Write-Host "[( $(Get-Timestamp) )] Stage 5 Finished."

# --- Final Report ---
Write-Host "`n========================================================="
Write-Host "BENCHMARK REPORT (10 Demos, $clipCount Clips)"
Write-Host "========================================================="
$total = 0
foreach ($s in $stages) {
    Write-Host ("{0,-25} : {1,8:F2}s" -f $s.Name, $s.Sec)
    $total += $s.Sec
}
Write-Host "---------------------------------------------------------"
Write-Host ("{0,-25} : {1,8:F2}s" -f "TOTAL", $total)
Write-Host ("(Average Render Time: {0,6:F2}s per clip)" -f ($stages[3].Sec / [math]::Max(1, $clipCount)))
Write-Host "========================================================="
