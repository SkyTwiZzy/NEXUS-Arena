@echo off
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "tools\backup_database.py"
) else (
    py -3 "tools\backup_database.py"
)
echo.
pause
