@echo off
chcp 65001 >nul
title Fast Download CLI

:: Auto-detect Python (try python3 first, then python)
set PYTHON=
for %%p in (python3 python) do (
    where %%p >nul 2>&1
    if not errorlevel 1 (
        set PYTHON=%%p
        goto :found
    )
)

echo [ERROR] Python not found in PATH. Please install Python 3.8+.
pause
exit /b 1

:found
echo [OK] Using %PYTHON%
echo.
"%PYTHON%" "%~dp0fast_download_cli.py"
pause
