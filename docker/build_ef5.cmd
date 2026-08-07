@echo off
REM Windows launcher — bypasses PowerShell execution-policy blocks
REM (common on mapped/network drives like X: and after GitHub clones).
REM
REM Usage (from repo root or docker\):
REM   docker\build_ef5.cmd
REM   docker\build_ef5.cmd -Status
REM   docker\build_ef5.cmd -Load
REM   docker\build_ef5.cmd -Rebuild
REM
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_ef5.ps1" %*
exit /b %ERRORLEVEL%
