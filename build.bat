@echo off
setlocal
title Build Wuwa Tappo Auto Claim
cd /d "%~dp0"

set "APP_ENTRY=main.py"
set "REQ_FILE=requirements.txt"
if not exist "%APP_ENTRY%" set "APP_ENTRY=source\main.py"
if not exist "%REQ_FILE%" set "REQ_FILE=source\requirements.txt"

echo ========================================================
echo   Building Wuwa Tappo Auto Claim with PyInstaller
echo ========================================================
echo.

if not exist "%APP_ENTRY%" (
    echo [ERROR] Could not find source\main.py or main.py.
    pause
    exit /b 1
)
if not exist "%REQ_FILE%" (
    echo [ERROR] Could not find source\requirements.txt or requirements.txt.
    pause
    exit /b 1
)
if not exist "Zani.ico" (
    echo [ERROR] Zani.ico is missing from the project folder.
    pause
    exit /b 1
)
if not exist "Zani.png" (
    echo [ERROR] Zani.png is missing from the project folder.
    pause
    exit /b 1
)

python -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

python -c "import pystray, psutil; from PIL import Image; import PyInstaller"
if errorlevel 1 (
    echo [ERROR] Required build/runtime modules are still missing.
    pause
    exit /b 1
)

python -m PyInstaller --onedir --windowed --uac-admin --name "WuwaTappoAutoClaim" --clean --noconfirm --icon "Zani.ico" --add-data "Zani.png;." "%APP_ENTRY%"
if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   Build Successful!
echo   Outputs located in dist\WuwaTappoAutoClaim\
echo ========================================================
echo.
pause
endlocal
