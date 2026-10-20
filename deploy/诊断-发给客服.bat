@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 出问题时的诊断：不开界面，把版本、代理、本机回环、窗口组件、最近日志写成一份 txt，
rem 然后用记事本打开。双击它，把打开的那个文件发给客服即可。
rem 这个文件必须是 CRLF 换行（见 docs/release.md）。
start "" "微信AI智能助理.exe" --diagnose
