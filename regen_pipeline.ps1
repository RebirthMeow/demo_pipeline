# regen_pipeline.ps1
# Single-shot pipeline:
#   1. sanity-check that the dm_meta backup tarballs exist
#   2. wipe every existing .dm_meta on disk
#   3. regenerate them with the freshly-built jkdemometadata.exe (parallelized)
#   4. re-run scanner.py once per player set, writing aggregated *_frags.json
#
# Run from a PowerShell prompt:
#   cd <repo_root>
#   .\regen_pipeline.ps1
#
# Optional flags:
#   -SkipBackupCheck     don't bail if backups are missing
#   -SkipMetaRegen       skip steps 1-3 (just rerun scanner.py for all sets)

param(
    [switch]$SkipBackupCheck,
    [switch]$SkipWipe,
    [switch]$SkipRegen,
    [switch]$SkipAggregate
)

$ErrorActionPreference = 'Stop'
# PS 7.3+ - make native command nonzero exit codes NOT throw.  Otherwise a
# single corrupt demo aborts the whole parallel regen.
if (Get-Variable -Name 'PSNativeCommandUseErrorActionPreference' -Scope Global -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$Root = if ($env:JACTF_ROOT) { $env:JACTF_ROOT } else { $PSScriptRoot }
$Exe  = "$Root\jkdemometadata.exe"
$ScannerPy = "$Root\scanner.py"
$DemosRoot = "$Root\python\trimming\demos source"

# Auto-discover player sets: every immediate subfolder of $DemosRoot that
# contains at least one .dm_26 anywhere underneath becomes a set.  Output JSON
# follows the convention <folder>_frags.json under python\predict.  Drop a new
# bucket like _jactf_new in there and it joins the pipeline with no edits.
$PlayerSets = @(
    Get-ChildItem -Path $DemosRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            (Get-ChildItem -LiteralPath $_.FullName -Recurse -Filter '*.dm_26' -File `
                           -ErrorAction SilentlyContinue | Select-Object -First 1)
        } |
        ForEach-Object {
            @{ Folder = $_.Name; Out = "$Root\python\predict\$($_.Name)_frags.json" }
        }
)
Write-Host ("    auto-discovered $($PlayerSets.Count) player set(s): " + `
            (($PlayerSets | ForEach-Object { $_.Folder }) -join ', ')) -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# 1. backup sanity check
# ---------------------------------------------------------------------------
if (-not $SkipBackupCheck) {
    Write-Host "==> step 1/4  verify backups exist" -ForegroundColor Cyan
    $backups = Get-ChildItem $Root -Filter 'dm_meta_backup_*.tar.gz' -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -notlike '*_BAD*' }
    if (-not $backups) {
        throw "No dm_meta_backup_*.tar.gz found in $Root. Run with -SkipBackupCheck if intentional."
    }
    $totalMB = [math]::Round((($backups | Measure-Object Length -Sum).Sum / 1MB), 1)
    Write-Host "    found $($backups.Count) backup parts, $totalMB MB total" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 2. wipe old .dm_meta files
# ---------------------------------------------------------------------------
if (-not $SkipWipe) {
    Write-Host "==> step 2/4  wipe old .dm_meta" -ForegroundColor Cyan
    # Scoped to demos source/ ONLY - never touch derived clips in
    # demos output/ or mme/demos/ (those don't have .dm_meta and shouldn't).
    $oldMeta = Get-ChildItem -Path $DemosRoot -Recurse -Filter '*.dm_meta' -ErrorAction SilentlyContinue
    if ($oldMeta.Count -gt 0) {
        Write-Host "    deleting $($oldMeta.Count) files..."
        $oldMeta | Remove-Item -Force
        Write-Host "    done" -ForegroundColor Green
    } else {
        Write-Host "    (none found, nothing to wipe)" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# 3. regenerate .dm_meta from every .dm_26 (parallel)
# ---------------------------------------------------------------------------
if (-not $SkipRegen) {
    Write-Host "==> step 3/4  regenerate .dm_meta via jkdemometadata.exe" -ForegroundColor Cyan
    if (-not (Test-Path $Exe)) {
        throw "Missing exe: $Exe (run build_metadata.bat install first)"
    }
    # Scope to demos source/ ONLY.  Derived clips live in demos output/
    # (trimmed by DemoTrimmer.exe) and mme/demos/ (staged f0001..fNNNN copies
    # for jaMME render) - neither needs .dm_meta and including them is
    # redundant work (~2-3k extra demos per stage cycle).
    $allDemos = Get-ChildItem -Path $DemosRoot -Recurse -Filter '*.dm_26' -File |
                Select-Object -ExpandProperty FullName
    Write-Host "    found $($allDemos.Count) .dm_26 demos in demos source/"

    # Resume mode: skip demos that already have a non-empty .dm_meta, so
    # re-running this script after a partial / aborted earlier run picks up
    # only the remaining work.  Use -LiteralPath because some demo filenames
    # contain literal `[` and `]` which the default -Path arg treats as
    # wildcards (and explodes when the pattern is malformed).
    $todo = $allDemos | Where-Object {
        $meta = "$_.dm_meta"
        if (Test-Path -LiteralPath $meta -PathType Leaf) {
            (Get-Item -LiteralPath $meta).Length -eq 0
        } else {
            $true
        }
    }
    Write-Host "    $($todo.Count) demos still need .dm_meta (others already done)"

    if ($todo.Count -eq 0) {
        Write-Host "    nothing to do" -ForegroundColor DarkGray
        $script:DirtySets = @()
    } else {
        $workers = [Environment]::ProcessorCount - 1
        if ($workers -lt 1) { $workers = 1 }
        $perfLog = "$Root\regen_perf.log"
        Write-Host "    processing with $workers workers (errors per demo are non-fatal)..."
        Write-Host "    per-demo wallclock -> $perfLog"

        # Header in perf log - append, don't truncate (keep history across runs)
        Add-Content -Path $perfLog -Value ""
        Add-Content -Path $perfLog -Value "# === regen run @ $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ($($todo.Count) demos, $workers workers) ==="

        $start = Get-Date
        # Sequential processing for compatibility with PS 5.1
        $todo | ForEach-Object {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            try {
                & $Exe $_ *> $null
            } catch {
                # individual demo failed - ignore and continue
            } finally {
                $sw.Stop()
            }
            $line = "{0,7:F2}s  {1}" -f $sw.Elapsed.TotalSeconds, $_
            Add-Content -Path $perfLog -Value $line
        }
        $elapsed = (Get-Date) - $start

        # Re-count what's actually on disk so we know the success rate
        $made = ($todo | Where-Object {
            $m = "$_.dm_meta"
            if (Test-Path -LiteralPath $m -PathType Leaf) {
                (Get-Item -LiteralPath $m).Length -gt 0
            } else {
                $false
            }
        }).Count
        $failed = $todo.Count - $made
        $rate = if ($elapsed.TotalSeconds -gt 0) { $made / $elapsed.TotalSeconds * 60 } else { 0 }
        Write-Host ("    done in {0:N1}s - produced {1} new .dm_meta, {2} failed  ({3:N1} demos/min)" `
                    -f $elapsed.TotalSeconds, $made, $failed, $rate) -ForegroundColor Green

        # Simplified summary for PS 5.1
        Write-Host "    (Sequential run finished)" -ForegroundColor DarkGray

        # Track which player-set folders this batch touched - downstream stages
        # use this so scanner.py only re-aggregates folders that actually changed.
        $script:DirtySets = $todo | ForEach-Object {
            $rel = $_.Substring($DemosRoot.Length).TrimStart('\','/')
            ($rel -split '[\\\/]', 2)[0]
        } | Sort-Object -Unique
    }
}

# ---------------------------------------------------------------------------
# 4. aggregate per-player-set
# ---------------------------------------------------------------------------
if ($SkipAggregate) {
    Write-Host "==> step 4/4  skipped (use without -SkipAggregate to run scanner.py)" -ForegroundColor DarkGray
    return
}
Write-Host "==> step 4/4  aggregate per-player-set with scanner.py" -ForegroundColor Cyan
$dirty = if ($null -ne $script:DirtySets) { @($script:DirtySets) } else { @() }
if ($dirty.Count -gt 0) {
    Write-Host ("    re-aggregating only sets that got new .dm_meta this run: " +
                ($dirty -join ', ')) -ForegroundColor DarkGray
} else {
    Write-Host "    no new .dm_meta this run - all sets up to date, skipping scanner" -ForegroundColor DarkGray
}
foreach ($s in $PlayerSets) {
    if ($dirty.Count -gt 0 -and ($dirty -notcontains $s.Folder)) {
        Write-Host "    [skip-clean] $($s.Folder)" -ForegroundColor DarkGray
        continue
    }
    if ($dirty.Count -eq 0) { continue }
    $folder = "$DemosRoot\$($s.Folder)"
    if (-not (Test-Path $folder)) {
        Write-Host "    [skip] $($s.Folder): folder not found" -ForegroundColor DarkGray
        continue
    }
    $glob = Join-Path $folder '**\*.dm_meta'
    Write-Host "    [$($s.Folder)] -> $(Split-Path -Leaf $s.Out)"
    & python $ScannerPy --input $glob --out $s.Out
}

Write-Host "==> all done" -ForegroundColor Cyan
