@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
title NEXUS Arena - Public HTTPS mode (server)
echo STEP 1/2: Start the server first. Keep this window open.
echo STEP 2/2: Run START_PUBLIC_LINK.bat in a SECOND window.
echo.
call "%~dp0START_NEXUS.bat" public
