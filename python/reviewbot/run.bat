@echo off
REM run.bat — bootstrap venv + launch the review bot.
REM
REM First run: creates .\venv\, installs discord.py.  Re-runs reuse it.
REM Requires DISCORD_TOKEN, DISCORD_GUILD_ID, DISCORD_CHANNEL_ID env vars.

setlocal
cd /d "%~dp0"

REM Load .env if it exists
if exist .env (
    for /f "usebackq tokens=1,2 delims==" %%A in (".env") do (
        set "%%A=%%B"
    )
)

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

if not defined DISCORD_TOKEN (
    echo ERROR: DISCORD_TOKEN env var not set.
    echo See .env.example for the full list of required vars.
    exit /b 1
)

echo.
echo Starting review bot.  Ctrl+C to stop.
echo.

venv\Scripts\python.exe bot.py %*

endlocal
