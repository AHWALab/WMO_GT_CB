@echo off
REM ============================================================================
REM reset_tito.cmd — wipe training run products (pure Windows CMD)
REM ============================================================================
REM   reset_tito.cmd
REM   reset_tito.cmd --dry-run
REM   reset_tito.cmd --force
REM
REM Wipes contents of outputs, precip, precipEF5, qpf_store, STREAM-Sat and
REM StormLab output folders. Keeps the folders themselves and .gitkeep.
REM
REM States: never delete folders. Only delete *.tif that are NOT the training
REM warmup snapshot (20230619 15:00). Matches:
REM   20230619_1500  20230619_150000  20230619.1500  20230619.150000  202306191500
REM If none match, state tifs are left alone (unless --force).
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "ROOT=%CD%"
set "DRY_RUN=0"
set "FORCE=0"
if /I "%~1"=="--dry-run" set "DRY_RUN=1"
if /I "%~1"=="-n" set "DRY_RUN=1"
if /I "%~1"=="/dry-run" set "DRY_RUN=1"
if /I "%~1"=="--force" set "FORCE=1"
if /I "%~1"=="-f" set "FORCE=1"
if /I "%~2"=="--force" set "FORCE=1"
if /I "%~2"=="--dry-run" set "DRY_RUN=1"

echo ==== reset_tito ====
echo   Root   : %ROOT%
if "%DRY_RUN%"=="1" (echo   Mode   : DRY-RUN) else (echo   Mode   : DELETE)
echo   Keep   : states *.tif for 2023-06-19 15:00 ^(any common spelling^)
echo.

call :wipe_dir "%ROOT%\outputs"
call :wipe_dir "%ROOT%\EF5_conf\precip"
call :wipe_dir "%ROOT%\EF5_conf\precipEF5"
call :wipe_dir "%ROOT%\EF5_conf\qpf_store"
call :wipe_dir "%ROOT%\tito_utils\qpe_utils\STREAM-Sat-realtime\extension\realtime\output\caribbean"
call :wipe_dir "%ROOT%\tito_utils\qpf_utils\StormLab-GFS-realtime\output\guatemala"

echo.
call :clean_states "%ROOT%\EF5_conf\states"

echo.
if "%DRY_RUN%"=="1" (
  echo Dry run only. Re-run without --dry-run to delete.
) else (
  echo Reset complete.
)
exit /b 0

:wipe_dir
set "DIR=%~1"
if not exist "%DIR%\" (
  echo SKIP ^(missing^): %DIR%
  exit /b 0
)
set "N=0"
for /d %%D in ("%DIR%\*") do (
  set /a N+=1
  if "%DRY_RUN%"=="1" (
    echo   WOULD REMOVE: %%D
  ) else (
    rd /s /q "%%D"
  )
)
for %%F in ("%DIR%\*") do (
  if /I not "%%~nxF"==".gitkeep" (
    set /a N+=1
    if "%DRY_RUN%"=="1" (
      echo   WOULD REMOVE: %%F
    ) else (
      del /q "%%F" >nul 2>&1
    )
  )
)
echo WIPE  %DIR%  ^(!N! item^(s^)^)
exit /b 0

:is_keep
set "KEEP_HIT=0"
echo %~1| findstr /I /L /C:"20230619_1500" /C:"20230619_150000" /C:"20230619.1500" /C:"20230619.150000" /C:"202306191500" >nul
if not errorlevel 1 set "KEEP_HIT=1"
exit /b 0

:clean_states
set "SDIR=%~1"
if not exist "%SDIR%\" (
  echo SKIP ^(missing^): %SDIR%
  exit /b 0
)
set "DELN=0"
set "KEEPN=0"
set "TOTAL=0"
set "SAMPLES="
for /r "%SDIR%" %%F in (*.tif) do (
  set /a TOTAL+=1
  call :is_keep "%%~nxF"
  if "!KEEP_HIT!"=="1" (
    set /a KEEPN+=1
    echo     KEEP  %%F
  ) else (
    set /a DELN+=1
    if !DELN! LEQ 8 set "SAMPLES=!SAMPLES!    %%~nxF&echo."
  )
)
for /r "%SDIR%" %%F in (*.tiff) do (
  set /a TOTAL+=1
  call :is_keep "%%~nxF"
  if "!KEEP_HIT!"=="1" (
    set /a KEEPN+=1
    echo     KEEP  %%F
  ) else (
    set /a DELN+=1
  )
)

echo STATES scan: !TOTAL! tif^(s^) — keep !KEEPN!  delete !DELN!

if !TOTAL! GTR 0 if !KEEPN! EQU 0 if not "%FORCE%"=="1" (
  echo ERROR: no state tif matched 2023-06-19 15:00 — refusing to delete any state tifs.
  echo   Sample names in %SDIR%:
  set "N=0"
  for /r "%SDIR%" %%F in (*.tif) do (
    if !N! LSS 8 (
      echo     %%~nxF
      set /a N+=1
    )
  )
  echo   Restore/check the warmup files, then re-run. Or pass --force to delete all non-matching tifs anyway.
  exit /b 0
)

for /r "%SDIR%" %%F in (*.tif) do (
  call :is_keep "%%~nxF"
  if "!KEEP_HIT!"=="0" (
    if "%DRY_RUN%"=="1" (
      echo   WOULD DELETE TIF: %%F
    ) else (
      del /q "%%F" >nul 2>&1
    )
  )
)
for /r "%SDIR%" %%F in (*.tiff) do (
  call :is_keep "%%~nxF"
  if "!KEEP_HIT!"=="0" (
    if "%DRY_RUN%"=="1" (
      echo   WOULD DELETE TIF: %%F
    ) else (
      del /q "%%F" >nul 2>&1
    )
  )
)
echo STATES %SDIR%  deleted !DELN! tif^(s^), kept !KEEPN!, folders untouched
exit /b 0
