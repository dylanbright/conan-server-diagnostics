@echo off
setlocal

rem ---------------------------------------------------------------------------
rem Bootstrap and run the Conan diagnostics service in a local venv.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

set "VENV_DIR=.venv"
set "PY_LAUNCHER=py -3.11"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [run] Creating virtualenv in %VENV_DIR% ...
    %PY_LAUNCHER% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [run] py -3.11 failed, trying plain python ...
        python -m venv "%VENV_DIR%"
        if errorlevel 1 (
            echo [run] ERROR: could not create virtualenv. Install Python 3.11+ and retry.
            exit /b 1
        )
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo [run] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip >nul
if errorlevel 1 (
    echo [run] ERROR: pip upgrade failed.
    exit /b 1
)

echo [run] Installing requirements ...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [run] ERROR: pip install failed.
    exit /b 1
)

if not exist ".env" (
    echo [run] WARNING: .env not found. Copy .env.example to .env and fill in your keys.
)

echo [run] Starting diagnostics.py ...
"%VENV_PY%" diagnostics.py
exit /b %errorlevel%
