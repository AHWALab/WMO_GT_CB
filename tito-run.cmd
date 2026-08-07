@echo off
REM ============================================================================
REM tito-run.cmd — pure Windows CMD launcher (no PowerShell)
REM ============================================================================
REM Avoids PowerShell execution-policy / Group Policy blocks entirely.
REM
REM Usage (from repo root, cmd.exe or double-click via tito-run.bat):
REM   tito-run.cmd load-images
REM   tito-run.cmd operational --regions Guatemala
REM   tito-run.cmd hindcast "2026-07-22 00:00" "2026-07-22 06:00" --regions Guatemala
REM   tito-run.cmd shell
REM
REM Env (optional):
REM   TITO_IMAGE          default tito:latest
REM   EF5_DOCKER_IMAGE    default ef5-container:latest
REM   TITO_SKIP_LOAD=1    do not auto-load missing images from dist\
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "ROOT=%CD%"

if not defined TITO_IMAGE set "TITO_IMAGE=tito:latest"
if not defined EF5_DOCKER_IMAGE set "EF5_DOCKER_IMAGE=ef5-container:latest"

REM ── help ────────────────────────────────────────────────────────────────
if /I "%~1"=="help" goto usage
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if /I "%~1"=="/?" goto usage

REM ── docker present? ─────────────────────────────────────────────────────
where docker >nul 2>&1
if errorlevel 1 (
  echo ERROR: docker not found in PATH.
  echo   1. Install Docker Desktop for Windows
  echo   2. Start Docker Desktop and wait until it is ready
  echo   3. Re-run this script
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo ERROR: cannot talk to the Docker daemon. Is Docker Desktop running?
  exit /b 1
)

REM ── load-images (force) ─────────────────────────────────────────────────
if /I "%~1"=="load-images" goto load_force
if /I "%~1"=="load_images" goto load_force
if /I "%~1"=="load" goto load_force

call :ensure_images 0
if errorlevel 1 exit /b 1
call :ensure_dirs

REM Pass %* straight through so quoted hindcast datetimes stay intact.
REM (Do not stash into a variable — CMD mangles quotes that way.)
if "%~1"=="" (
  call :run_tito operational
) else (
  call :run_tito %*
)
exit /b %ERRORLEVEL%

:load_force
call :ensure_images 1
if errorlevel 1 exit /b 1
echo.
echo Loaded images:
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
exit /b 0

REM ========================================================================
REM  Subroutines
REM ========================================================================

:ensure_dirs
if not exist "%ROOT%\EF5_conf" mkdir "%ROOT%\EF5_conf"
if not exist "%ROOT%\outputs" mkdir "%ROOT%\outputs"
for %%D in (basic parameters pet templates states precip precipEF5 qpf_store) do (
  if not exist "%ROOT%\EF5_conf\%%D" mkdir "%ROOT%\EF5_conf\%%D"
)
exit /b 0

:ensure_images
REM %1 = 1 force load both, 0 only if missing
set "FORCE=%~1"
if "%TITO_SKIP_LOAD%"=="1" if not "%FORCE%"=="1" exit /b 0

set "NEED_TITO=0"
set "NEED_EF5=0"
if "%FORCE%"=="1" (
  set "NEED_TITO=1"
  set "NEED_EF5=1"
) else (
  docker image inspect "%TITO_IMAGE%" >nul 2>&1
  if errorlevel 1 set "NEED_TITO=1"
  docker image inspect "%EF5_DOCKER_IMAGE%" >nul 2>&1
  if errorlevel 1 set "NEED_EF5=1"
)

if "%NEED_TITO%"=="0" if "%NEED_EF5%"=="0" exit /b 0

echo ==== Loading Docker images from dist\ (USB / pre-built) ====

if "%NEED_TITO%"=="1" (
  call :find_archive "tito_latest.tar.gz" "tito_latest.tar"
  if not defined FOUND_ARCHIVE (
    echo ERROR: Missing %TITO_IMAGE% and archive not found.
    echo   Place dist\docker-archives\tito_latest.tar.gz then:
    echo     tito-run.cmd load-images
    exit /b 1
  )
  echo     Loading %TITO_IMAGE% from:
  echo       !FOUND_ARCHIVE!
  docker load -i "!FOUND_ARCHIVE!"
  if errorlevel 1 (
    echo ERROR: docker load failed for !FOUND_ARCHIVE!
    exit /b 1
  )
)

if "%NEED_EF5%"=="1" (
  call :find_archive "ef5-container_latest.tar.gz" "ef5-container_latest.tar"
  if not defined FOUND_ARCHIVE (
    echo ERROR: Missing %EF5_DOCKER_IMAGE% and archive not found.
    echo   Place dist\docker-archives\ef5-container_latest.tar.gz then:
    echo     tito-run.cmd load-images
    exit /b 1
  )
  echo     Loading %EF5_DOCKER_IMAGE% from:
  echo       !FOUND_ARCHIVE!
  docker load -i "!FOUND_ARCHIVE!"
  if errorlevel 1 (
    echo ERROR: docker load failed for !FOUND_ARCHIVE!
    exit /b 1
  )
)

echo     Images ready.
exit /b 0

:find_archive
REM %1 preferred name (e.g. tito_latest.tar.gz), %2 fallback (.tar)
set "FOUND_ARCHIVE="
set "NAME_GZ=%~1"
set "NAME_TAR=%~2"
for %%D in (
  "%ROOT%\dist\docker-archives"
  "%ROOT%\dist\docker-images"
  "%ROOT%\dist"
  "%ROOT%"
) do (
  if not defined FOUND_ARCHIVE if exist "%%~D\!NAME_GZ!" set "FOUND_ARCHIVE=%%~D\!NAME_GZ!"
  if not defined FOUND_ARCHIVE if exist "%%~D\!NAME_TAR!" set "FOUND_ARCHIVE=%%~D\!NAME_TAR!"
)
exit /b 0

:run_tito
REM Args after call :run_tito are the TITO command line (operational / hindcast / …)
docker image inspect "%TITO_IMAGE%" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Image missing: %TITO_IMAGE% — run: tito-run.cmd load-images
  exit /b 1
)
docker image inspect "%EF5_DOCKER_IMAGE%" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Image missing: %EF5_DOCKER_IMAGE% — run: tito-run.cmd load-images
  exit /b 1
)

echo ==== TITO launcher ====
echo   Runtime : docker (windows CMD)
echo   Project : %ROOT%
echo   Image   : %TITO_IMAGE%
echo   EF5     : %EF5_DOCKER_IMAGE% (sibling via docker.sock)
echo   Network : bridge (Docker Desktop)

REM No --network host on Docker Desktop Windows
docker run --rm ^
  -v "%ROOT%\EF5_conf:/app/EF5_conf" ^
  -v "%ROOT%\outputs:/app/outputs" ^
  -v "%ROOT%\Caribbean_Comoros_config.py:/app/Caribbean_Comoros_config.py:ro" ^
  -v "%ROOT%\orchestrator.py:/app/orchestrator.py:ro" ^
  -v "%ROOT%\hindcast_manager.py:/app/hindcast_manager.py:ro" ^
  -v "%ROOT%\tito_utils:/app/tito_utils" ^
  -v "%ROOT%\EF5:/app/EF5:ro" ^
  -v "/var/run/docker.sock:/var/run/docker.sock" ^
  -e "EF5_RUNTIME=docker" ^
  -e "EF5_IMAGE=%EF5_DOCKER_IMAGE%" ^
  -e "TITO_HOST_PROJECT=%ROOT%" ^
  -e "PYTHONUNBUFFERED=1" ^
  -e "TZ=Etc/UTC" ^
  -e "STORMLAB_USE_TITO_ENV=1" ^
  "%TITO_IMAGE%" %*
exit /b %ERRORLEVEL%

:usage
echo.
echo tito-run.cmd — pure Windows CMD (no PowerShell^)
echo.
echo Usage:
echo   tito-run.cmd load-images
echo   tito-run.cmd operational --regions Guatemala
echo   tito-run.cmd hindcast "2026-07-22 00:00" "2026-07-22 06:00" --regions Guatemala
echo   tito-run.cmd shell
echo.
echo Place pre-built archives (USB^) at:
echo   dist\docker-archives\tito_latest.tar.gz
echo   dist\docker-archives\ef5-container_latest.tar.gz
echo.
echo Optional env:
echo   set TITO_IMAGE=tito:latest
echo   set EF5_DOCKER_IMAGE=ef5-container:latest
echo   set TITO_SKIP_LOAD=1
echo.
exit /b 0
