@echo off
cd /d "%~dp0"
title VidLingua

if not exist ".venv\Scripts\python.exe" (
    echo Python virtual environment not found: .venv\Scripts\python.exe
    echo See README.md for setup instructions.
    pause
    exit /b 1
)

set "VIDLINGUA_PORT=%~1"
if "%VIDLINGUA_PORT%"=="" set "VIDLINGUA_PORT=8001"
echo VidLingua is starting at http://127.0.0.1:%VIDLINGUA_PORT%
echo Close this window to stop the server.
echo.

start "" cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:%VIDLINGUA_PORT%"
".venv\Scripts\python.exe" -m uvicorn web_app:app --host 127.0.0.1 --port %VIDLINGUA_PORT%
echo.
echo Server stopped.
pause
