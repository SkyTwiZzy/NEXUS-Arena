@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title NEXUS Arena - Public internet link
set "CF_BIN="
if exist "%~dp0cloudflared.exe" set "CF_BIN=%~dp0cloudflared.exe"
if not defined CF_BIN (
    where cloudflared.exe >nul 2>nul
    if not errorlevel 1 set "CF_BIN=cloudflared.exe"
)
if not defined CF_BIN if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CF_BIN=%ProgramFiles%\cloudflared\cloudflared.exe"
if not defined CF_BIN if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CF_BIN=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not defined CF_BIN goto missing_cloudflared
powershell -NoProfile -Command "try {$s=Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/status' -TimeoutSec 5; if ($s.online -eq $true) {exit 0} else {exit 1}} catch {exit 1}"
if errorlevel 1 goto no_server
echo.
echo ================================================================
echo  PUBLIC HTTPS LINK - TEMPORARY TEST ACCESS

echo  The link ending in trycloudflare.com appears below.
echo  Copy it and send to someone you trust to test your site.
echo  Keep BOTH server and this window running; link changes on restart.
echo  This is not permanent production hosting.
echo ================================================================
echo.
"%CF_BIN%" tunnel --url http://127.0.0.1:8000
if errorlevel 1 goto tunnel_error
goto done
:missing_cloudflared
echo ERROR: Cloudflare Tunnel tool is not installed.
echo Install it from the official Cloudflare documentation:
echo https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/
echo.
echo OR run in PowerShell: winget install --id Cloudflare.cloudflared -e
echo OR put the official cloudflared.exe file NEXT TO this batch file.
echo After installation, close and reopen the console and retry.
goto failed
:no_server
echo ERROR: NEXUS server not found at http://127.0.0.1:8000/api/status
echo First run START_NEXUS_PUBLIC_SERVER.bat and leave it open.
goto failed
:tunnel_error
echo ERROR: Cloudflare Tunnel failed. Check Internet and the text above.
goto failed
:failed
pause
exit /b 1
:done
pause
exit /b 0
