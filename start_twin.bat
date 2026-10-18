@echo off
rem Starts the digital twin in its own small window (double-click).
rem Opens the classic console (conhost) because Windows 11 Terminal ignores the window size.
if "%1"=="inner" goto run
start "" conhost.exe cmd /c ""%~f0" inner"
exit /b

:run
title Digital twin - hotend
mode con: cols=110 lines=14
cd /d "%~dp0"
python realtime_websocket.py
echo.
echo Monitoring stopped. Summary above. Press any key to close this window.
pause >nul
