@echo off
chcp 65001 >nul
rem One-click acceptance: tests + static checks + symptom guards + client self-check + UI contract.
rem ASCII only on purpose: cmd parses .bat bytes with the OEM code page, and Chinese
rem comment lines get mis-split (they even run as commands). The Python tool prints
rem the Chinese output, and chcp 65001 above makes that render correctly.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   .venv not found. See QuickStart.md to create it and install dependencies.
    echo.
    pause
    goto :done
)

".venv\Scripts\python.exe" -X utf8 examples\acceptance.py %*

echo.
echo Note: code + UI contract only. Real WeChat / real model checks are separate
echo (see the acceptance list in docs\merchant-software.md, section 8).
pause
:done
