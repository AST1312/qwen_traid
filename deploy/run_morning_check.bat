@echo off
setlocal
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"
if "%TG_PROXY%"=="" set "TG_PROXY=socks5://127.0.0.1:10808"
set "LOG_DIR=%PROJECT_DIR%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\watch-scheduler.log"
echo. >> "%LOG_FILE%"
echo === %date% %time% === Start morning check === >> "%LOG_FILE%"
C:\Windows\py.exe scripts\watch.py >> "%LOG_FILE%" 2>&1
echo === %date% %time% === Exit code %ERRORLEVEL% === >> "%LOG_FILE%"
exit /b %ERRORLEVEL%
