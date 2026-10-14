@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot open the project folder. Extract the ZIP to C:\NEXUS_Arena_PC first.
    pause
    exit /b 1
)
title NEXUS Arena - Local server
if not exist "logs" mkdir "logs"
set "INSTALL_LOG=%CD%\logs\install.log"
set "SERVER_LOG=%CD%\logs\server.log"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_DEFAULT_TIMEOUT=30"

echo ==================================================
echo        NEXUS ARENA - WINDOWS v1.1
echo ==================================================
echo.
echo Install log: "%INSTALL_LOG%"
echo.

rem Find ANY supported Python already installed on this computer.
rem The old launcher incorrectly required Python 3.12, even if Python 3.13 was installed.
set "PYTHON_LAUNCHER="
where py >nul 2>nul
if not errorlevel 1 (
    for %%V in (3.13 3.12 3.11 3.10 3.14) do (
        if not defined PYTHON_LAUNCHER (
            py -%%V -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,15)" >nul 2>nul
            if not errorlevel 1 set "PYTHON_LAUNCHER=py -%%V"
        )
    )
    if not defined PYTHON_LAUNCHER (
        py -3 -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,15)" >nul 2>nul
        if not errorlevel 1 set "PYTHON_LAUNCHER=py -3"
    )
)
if not defined PYTHON_LAUNCHER (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,15)" >nul 2>nul
        if not errorlevel 1 set "PYTHON_LAUNCHER=python"
    )
)
if not defined PYTHON_LAUNCHER (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        python3 -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,15)" >nul 2>nul
        if not errorlevel 1 set "PYTHON_LAUNCHER=python3"
    )
)
if not defined PYTHON_LAUNCHER goto no_python

echo [1/3] Python detected: %PYTHON_LAUNCHER%
%PYTHON_LAUNCHER% --version
if errorlevel 1 goto python_failed

echo.
echo [2/3] Checking Python virtual environment...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo Existing environment is damaged; recreating it.
        rmdir /s /q ".venv" >nul 2>nul
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment. See logs\install.log for details.
    %PYTHON_LAUNCHER% -m venv .venv >"%INSTALL_LOG%" 2>&1
    if errorlevel 1 goto venv_failed
)

".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo Restoring pip...
    ".venv\Scripts\python.exe" -m ensurepip --upgrade >>"%INSTALL_LOG%" 2>&1
    if errorlevel 1 goto pip_failed
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pydantic" >nul 2>nul
if errorlevel 1 (
    echo Installing server dependencies... Internet access required.
    echo The detailed installation output is in logs\install.log.
    ".venv\Scripts\python.exe" -m pip install -r "server\requirements.txt" --prefer-binary --retries 3 --timeout 30 --no-input >"%INSTALL_LOG%" 2>&1
    if errorlevel 1 goto install_failed
) else (
    echo Python dependencies are already installed.
)

echo.
echo [3/3] Starting NEXUS Arena...
echo Open http://127.0.0.1:8000 if your browser does not open automatically.
echo DO NOT close this window while using the site.
echo.
if /I "%~1"=="developer" (
    ".venv\Scripts\python.exe" "tools\launch_local.py" --developer
) else if /I "%~1"=="public" (
    ".venv\Scripts\python.exe" "tools\launch_local.py" --public
) else (
    ".venv\Scripts\python.exe" "tools\launch_local.py"
)
if errorlevel 1 goto server_failed
goto finish

:no_python
echo ERROR: A working Python 3.10 - 3.14 installation was not found.
echo Install it from https://www.python.org/downloads/
echo For a new Python install choose Python 3.12 or 3.13.
echo Tick "Add python.exe to PATH" and try again.
echo To see installed versions, open Command Prompt and run: py --list-paths
goto failed
:python_failed
echo ERROR: Python was detected but could not start.
goto failed
:venv_failed
echo ERROR: Could not create .venv environment.
echo Possible causes: antivirus, file permissions, Python installation.
goto show_install_error
:pip_failed
echo ERROR: pip is missing and automatic restoration failed.
goto show_install_error
:install_failed
echo ERROR: Dependency installation failed at step 2 of 3.
echo Causes may include network, SSL certificates, proxy or antivirus.
goto show_install_error
:show_install_error
echo.
echo ============ INSTALL ERROR LOG ============
if exist "%INSTALL_LOG%" type "%INSTALL_LOG%"
echo ============ END OF ERROR LOG ============
echo.
echo Send ONLY the error text or a screenshot; do not send passwords.
goto failed
:server_failed
echo ERROR: Server could not start. See the error above.
echo Check whether port 8000 is occupied and Python dependencies are installed.
goto failed
:failed
echo.
echo START FAILED. The window will stay open until you press a key.
echo TIP: Use START_NEXUS_DIAGNOSTIC.bat to keep the console open.
pause
exit /b 1
:finish
echo.
echo NEXUS Arena server stopped. You can close this window.
pause
exit /b 0
