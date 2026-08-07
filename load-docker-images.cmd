@echo off
REM Load pre-built TITO + EF5 images from dist\ (USB / pendrive).
REM Pure CMD wrapper around: tito-run.cmd load-images
setlocal
cd /d "%~dp0"
call "%~dp0tito-run.cmd" load-images %*
exit /b %ERRORLEVEL%
