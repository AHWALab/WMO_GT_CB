@echo off
REM Double-click friendly entry → pure CMD loader
setlocal
cd /d "%~dp0"
call "%~dp0load-docker-images.cmd" %*
exit /b %ERRORLEVEL%
