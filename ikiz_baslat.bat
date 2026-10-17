@echo off
rem Dijital ikizi kendi kucuk penceresinde baslatir (cift tikla).
rem Windows 11'de varsayilan terminal pencere boyutunu yok saydigi icin klasik konsol (conhost) acilir.
if "%1"=="ic" goto calis
start "" conhost.exe cmd /c ""%~f0" ic"
exit /b

:calis
title Dijital Ikiz - hotend
mode con: cols=110 lines=14
cd /d "%~dp0"
python realtime_websocket.py
echo.
echo Izleme bitti. Ozet yukarida. Pencereyi kapatmak icin bir tusa bas.
pause >nul
