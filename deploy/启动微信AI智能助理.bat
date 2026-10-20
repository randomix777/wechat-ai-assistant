@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 用 start 启动：本批处理立刻结束，控制台窗口跟着消失；
rem 直接写 "微信AI智能助理.exe" 会让 cmd 一直等它退出，黑框就挂在整个使用期间。
start "" "微信AI智能助理.exe" %*
