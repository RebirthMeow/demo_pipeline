@echo off
REM render_resilient.bat — wraps render_clips.py.
REM
REM This is the recommended entry point for the full clip batch.  It:
REM   - reads mme/clip_manifest.json (from build_jamme_demolist.py)
REM   - tracks per-clip state in mme/render_state.json
REM   - skips clips already rendered, retries crashes, times out hangs
REM   - safe to Ctrl+C and re-run; resumes where it left off
REM
REM Pass-through args:
REM   render_resilient.bat --status               (just print state, don't render)
REM   render_resilient.bat --limit 5              (smoke-test 5 clips)
REM   render_resilient.bat --only f0042 f0099     (render specific clips)
REM   render_resilient.bat --reset                (forget state, render all)
REM   render_resilient.bat --timeout 240          (longer per-clip timeout)
REM   render_resilient.bat --max-retries 5        (more retries per clip)

cd /d "%~dp0"

if not exist jamme.exe (
    echo ERROR: jamme.exe not found in %CD%
    pause
    exit /b 1
)

REM prefer "python", fall back to "py"
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    python "C:\jactf_pipeline\python\predict\render_clips.py" %*
) else (
    py "C:\jactf_pipeline\python\predict\render_clips.py" %*
)
