@echo off
REM Windows double-click / cmd.exe entry → reset_tito.cmd
setlocal
cd /d "%~dp0"
call "%~dp0reset_tito.cmd" %*
exit /b %ERRORLEVEL%
