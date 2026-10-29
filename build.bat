@echo off
title Build Wuwa Tappo Auto Claim
cd /d "%~dp0"

echo ========================================================
echo   Building Wuwa Tappo Auto Claim with PyInstaller
echo ========================================================
echo.

python -m pip install -r source\requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b %errorlevel%
)

pyinstaller --onedir --windowed --name "WuwaTappoAutoClaim" --clean --noconfirm source\main.py
if %errorlevel% neq 0 (
    echo [ERROR] Build failed.
    pause
    exit /b %errorlevel%
)

echo.
echo ========================================================
echo   Build Successful!
echo   Outputs located in dist\WuwaTappoAutoClaim\
echo ========================================================
echo.
pause
