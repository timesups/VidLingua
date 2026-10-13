@echo off
cd /d "%~dp0"
title VidLingua

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境：.venv\Scripts\python.exe
    echo 请先按 README 创建 Python 3.12 虚拟环境并安装依赖。
    pause
    exit /b 1
)

echo VidLingua 正在启动
echo 地址：http://127.0.0.1:8000
echo 关闭本窗口即可停止服务。
echo.

start "" cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:8000"

".venv\Scripts\python.exe" -m uvicorn web_app:app --host 127.0.0.1 --port 8000
echo.
echo 服务已停止。
pause