# run_pipeline.ps1
#
# Top-level end-to-end pipeline.  Chains every stage so one command takes
# you from "matches uploaded to JACTF" -> "mp4 highlight clips on disk".
# Each stage is idempotent - safe to re-run any time.
#
# Run from a PowerShell prompt:
#   cd <repo_root>
#   .\run_pipeline.ps1
#
# Optional flags (skip individual stages):
#   -SkipFetch       skip pulling new demos from JACTF
#   -SkipRegen       skip jkdemometadata + scanner
#   -SkipPredict     skip predict_frags_ensemble.py
#   -SkipStage       skip build_jamme_demolist.py
#   -SkipRender      skip render_resilient.bat
#
# Tuning:
#   -Since <expr>    fetcher window LOWER bound (default '1d').  Nh / Nd / Nw / Nm or YYYY-MM-DD.
#   -Until <expr>    fetcher window UPPER bound (default '0d' = now).  Same formats.
#   -DryFetch        run fetcher with --dry-run (scrape only, no downloads)
#   -MaxMatches N    cap N new matches to download this run
#   -LimitClips  N   cap N clips to render in the smoke-test
#   -ThresholdMode <default|high-precision>   for predict stage
#
# HARD SAFETY: fetcher refuses any window > 30 days per invocation.  To
# backfill further, run multiple times with explicit windows:
#     .\run_pipeline.ps1 -Since 30d                    # last 30 days
#     .\run_pipeline.ps1 -Since 60d -Until 30d         # days 30-60
#     .\run_pipeline.ps1 -Since 90d -Until 60d         # days 60-90
#
# Examples:
#   .\run_pipeline.ps1 -MaxMatches 5 -LimitClips 5     # tiny end-to-end test
#   .\run_pipeline.ps1 -SkipFetch                      # re-run without fetching
#   .\run_pipeline.ps1 -SkipRender                     # everything but render
param(
    [switch]$SkipFetch,
    [switch]$SkipRegen,
    [switch]$SkipPredict,
    [switch]$SkipStage,
    [switch]$SkipRender,
    [switch]$DryFetch,
    [string]$Since = '',
    [string]$Until = '',
    [int]   $MaxMatches = 0,
    [int]   $LimitClips = 0,
    [ValidateSet('default','high-precision')]
    [string]$ThresholdMode = 'default'
)

$ErrorActionPreference = 'Stop'

# $Root resolves to this script's directory by default.  Override with
# JACTF_ROOT env var if you've moved the orchestrator out of the repo root.
$Root        = if ($env:JACTF_ROOT) { $env:JACTF_ROOT } else { $PSScriptRoot }
# GameData defaults to in-repo; override with JACTF_GAMEDATA on machines where
# jaMME lives elsewhere (most users will need to set this).
$GameData    = if ($env:JACTF_GAMEDATA) { $env:JACTF_GAMEDATA } `
                else { "$Root\a full jka install\game_directory\Jedi Academy\GameData" }

$FetchPy     = "$Root\python\fetch\fetch_jactf_demos.py"
$RegenPs1    = "$Root\regen_pipeline.ps1"
$PredictPy   = "$Root\python\predict\predict_frags_ensemble.py"
$StagePy     = "$Root\python\predict\build_jamme_demolist.py"
$PredictDir  = "$Root\python\predict"
$RenderBat   = "$GameData\render_resilient.bat"

$T_start = Get-Date
function Step([string]$name) {
    Write-Host "`n=========================================================" -ForegroundColor Cyan
    Write-Host "==> $name" -ForegroundColor Cyan
    Write-Host "=========================================================" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 0. fetch new demos from JACTF
# ---------------------------------------------------------------------------
if (-not $SkipFetch) {
    Step "[0/5]  fetch new demos from JACTF"
    if (-not (Test-Path $FetchPy)) { throw "missing: $FetchPy" }
    $a = @($FetchPy)
    if ($DryFetch)           { $a += '--dry-run' }
    if ($Since)              { $a += '--since'; $a += $Since }
    if ($Until)              { $a += '--until'; $a += $Until }
    if ($MaxMatches -gt 0)   { $a += '--max-matches'; $a += $MaxMatches }
    & python @a
    if ($LASTEXITCODE -ne 0) { throw "fetch_jactf_demos.py failed (exit $LASTEXITCODE)" }
} else {
    Write-Host "[0/5]  fetch (skipped)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 1+2. extract per-frag metadata (.dm_meta) and aggregate per player set
# ---------------------------------------------------------------------------
# Take a "before" snapshot so stages 3 + 4 can identify what was freshly
# produced this run and ignore stale files from earlier experiments.
$preRegenTime = Get-Date

if (-not $SkipRegen) {
    Step "[1-2/5]  jkdemometadata regen + scanner aggregate"
    if (-not (Test-Path $RegenPs1)) { throw "missing: $RegenPs1" }
    # SkipBackupCheck because we already verified backups during initial setup;
    # SkipWipe because we want resume mode (only generate .dm_meta for demos
    # that don't yet have one - perfect for incremental runs).
    & $RegenPs1 -SkipBackupCheck -SkipWipe
    if ($LASTEXITCODE -ne 0) { throw "regen_pipeline.ps1 failed (exit $LASTEXITCODE)" }
} else {
    Write-Host "[1-2/5]  regen (skipped)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 3. predict - run only on *_frags.json files that scanner.py freshly produced.
#    (Old / orphan frags.json files from prior experiments - e.g. 3xen_frags
#     or down_frags with no matching demos source/ folder - are ignored.)
# ---------------------------------------------------------------------------
$freshCsvs = @()
if (-not $SkipPredict) {
    Step "[3/5]  predict_frags_ensemble.py"
    if (-not (Test-Path $PredictPy)) { throw "missing: $PredictPy" }
    $jsons = Get-ChildItem -Path $PredictDir -Filter '*_frags.json' -File `
                           -ErrorAction SilentlyContinue |
             Where-Object { $_.LastWriteTime -ge $preRegenTime }
    if (-not $jsons) {
        Write-Host "    no fresh *_frags.json this run - nothing to predict" -ForegroundColor DarkGray
        Write-Host "    (any predict CSVs from older runs are left untouched)" -ForegroundColor DarkGray
    } else {
        Write-Host ("    fresh inputs: " + (($jsons | ForEach-Object { $_.Name }) -join ', ')) -ForegroundColor DarkGray
        foreach ($j in $jsons) {
            $csv = Join-Path $j.DirectoryName ($j.BaseName + '.ensemble_predictions.csv')
            Write-Host ("    [predict]    {0}  (mode={1})" -f $j.Name, $ThresholdMode) -ForegroundColor Yellow
            & python $PredictPy $j.FullName --threshold-mode $ThresholdMode
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "predict failed for $($j.Name) - continuing"
            } elseif (Test-Path -LiteralPath $csv) {
                $freshCsvs += $csv
            }
        }
    }
} else {
    Write-Host "[3/5]  predict (skipped)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 4. build jaMME demolist + manifest - only consume CSVs we just produced.
# ---------------------------------------------------------------------------
if (-not $SkipStage) {
    Step "[4/5]  build_jamme_demolist.py"
    if (-not (Test-Path $StagePy)) { throw "missing: $StagePy" }
    if ($freshCsvs.Count -eq 0) {
        Write-Host "    no fresh predict CSVs - nothing to stage" -ForegroundColor DarkGray
    } else {
        Write-Host ("    consuming: " + (($freshCsvs | ForEach-Object { Split-Path -Leaf $_ }) -join ', ')) -ForegroundColor DarkGray
        $a = @($StagePy, '--csv') + $freshCsvs
        if ($LimitClips -gt 0) { $a += '--limit'; $a += $LimitClips }
        & python @a
        if ($LASTEXITCODE -ne 0) { throw "build_jamme_demolist.py failed (exit $LASTEXITCODE)" }
    }
} else {
    Write-Host "[4/5]  stage (skipped)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 5. render every clip to mp4 (resilient runner - resumes on crash)
# ---------------------------------------------------------------------------
if (-not $SkipRender) {
    Step "[5/5]  render_resilient.bat"
    if (-not (Test-Path $RenderBat)) { throw "missing: $RenderBat" }
    Push-Location $GameData
    try {
        $a = @()
        if ($LimitClips -gt 0) { $a += '--limit'; $a += $LimitClips }
        & $RenderBat @a
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "render_resilient.bat exited $LASTEXITCODE (some clips may be in 'failed' state - re-run to retry)"
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "[5/5]  render (skipped)" -ForegroundColor DarkGray
}

$elapsed = (Get-Date) - $T_start
Write-Host ""
Write-Host ("==> pipeline finished in {0:N1} min" -f $elapsed.TotalMinutes) -ForegroundColor Cyan
Write-Host "    captures: $GameData\mme\captures\" -ForegroundColor Cyan
