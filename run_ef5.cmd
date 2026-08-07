@echo off
REM Windows launcher — bypasses PowerShell execution-policy blocks
REM (common on mapped/network drives like X: and after GitHub clones).
REM
REM Usage (from repo root):
REM   run_ef5.cmd
REM   run_ef5.cmd -Control control_900m.txt
REM   run_ef5.cmd -Control control_90m_cuenca.txt
REM   run_ef5.cmd -Bash
REM
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_ef5.ps1" %*
exit /b %ERRORLEVEL%
