@echo off
REM Windows double-click / cmd.exe entry → pure CMD launcher (no PowerShell)
setlocal
cd /d "%~dp0"
call "%~dp0tito-run.cmd" %*
exit /b %ERRORLEVEL%
