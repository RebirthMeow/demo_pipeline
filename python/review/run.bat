@echo off
REM run.bat — launch the review UI on http://127.0.0.1:5057/
REM
REM First-time setup creates a per-tool venv at .\venv\ so Flask doesn't
REM pollute the predict/scanner venvs.  Re-runs reuse it.
REM
REM Pass-through: any args go straight to app.py (currently none defined,
REM but reserved for things like --port / --no-browser / --reset-state).

setlocal
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
    echo [setup] creating venv at %CD%\venv
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: python -m venv failed.  Is Python on PATH?
        exit /b 1
    )
    echo [setup] installing requirements
    venv\Scripts\python.exe -m pip install --upgrade pip >nul
    venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: pip install failed.
        exit /b 1
    )
)

echo.
echo Review UI starting on http://127.0.0.1:5057/
echo Press Ctrl+C to stop.
echo.

venv\Scripts\python.exe app.py %*

endlocal
