@echo off
cd /d "%~dp0"
echo.
echo ============================================================
echo   Fast Download CLI v3
echo   HTTP/HTTPS/Thunder/Magnet/BT
echo ============================================================
echo.
echo Checking Python...
python --version 2>nul
if errorlevel 1 goto :no_python
echo.
echo Starting...
echo.

python "%~dp0fast_download_cli.py"
pause
exit /b 0

:no_python
echo.
echo [ERROR] Python not found!
echo Please install Python 3.8+: https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
echo.
pause
exit /b 1
