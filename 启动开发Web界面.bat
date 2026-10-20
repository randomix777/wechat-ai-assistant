@echo off
chcp 65001 >nul
rem 开发用：启动 Web 调试界面（不是给商户的客户端）。
rem 改 src\wechat_ai_assistant\web_page.py 保存后，浏览器会自己刷新，不用重启。
rem 想加参数直接跟在后面，例如：启动开发Web界面.bat --data-dir "D:\某个数据目录"
setlocal
set "APPDIR=%~dp0"
cd /d "%APPDIR%"
set "PYTHONPATH=%APPDIR%src"

if not exist "%APPDIR%.venv\Scripts\python.exe" (
    echo.
    echo   没有找到 .venv，请先按 QuickStart.md 建好虚拟环境并装好依赖。
    echo.
    pause
    goto :done
)

".venv\Scripts\python.exe" -X utf8 -m wechat_ai_assistant.web_dev --open %*

:done
endlocal
