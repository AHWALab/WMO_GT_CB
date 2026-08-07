@echo off
REM ============================================================================
REM run_ef5.cmd — pure Windows CMD (no PowerShell)
REM ============================================================================
REM Avoids PowerShell execution-policy / Group Policy blocks entirely.
REM
REM Usage (from repo root):
REM   run_ef5.cmd
REM   run_ef5.cmd -Control control_900m.txt
REM   run_ef5.cmd -Control control_90m_cuenca.txt
REM   run_ef5.cmd -Bash
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "ROOT=%CD%"
if defined EF5_IMAGE (
  set "IMAGE=%EF5_IMAGE%"
) else (
  set "IMAGE=ef5-container:latest"
)
set "CONTROL=control_900m.txt"
set "DO_BASH=0"

:parse
if "%~1"=="" goto parsed
if /I "%~1"=="-Control" (
  if "%~2"=="" (
    echo ERROR: -Control requires a file name
    exit /b 1
  )
  set "CONTROL=%~2"
  shift & shift & goto parse
)
if /I "%~1"=="--control" (
  if "%~2"=="" (
    echo ERROR: --control requires a file name
    exit /b 1
  )
  set "CONTROL=%~2"
  shift & shift & goto parse
)
if /I "%~1"=="-Bash" set "DO_BASH=1" & shift & goto parse
if /I "%~1"=="--bash" set "DO_BASH=1" & shift & goto parse
if /I "%~1"=="-b" set "DO_BASH=1" & shift & goto parse
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if /I "%~1"=="/?" goto usage
REM allow bare control name as first arg: run_ef5.cmd control_90m_cuenca.txt
if not "%~1"=="" if "%CONTROL%"=="control_900m.txt" if "%DO_BASH%"=="0" (
  set "CONTROL=%~1"
  shift & goto parse
)
echo Unknown option: %~1
goto usage

:parsed
where docker >nul 2>&1
if errorlevel 1 (
  echo ERROR: docker not found in PATH. Install Docker Desktop first.
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo ERROR: cannot talk to the Docker daemon. Is Docker Desktop running?
  exit /b 1
)

REM Ensure image exists (reuse / load / build via pure CMD)
docker image inspect "%IMAGE%" >nul 2>&1
if errorlevel 1 (
  echo Image %IMAGE% not found - preparing it...
  call "%ROOT%\docker\build_ef5.cmd"
  if errorlevel 1 exit /b 1
)

if not defined OMP_NUM_THREADS (
  set "OMP_NUM_THREADS=%NUMBER_OF_PROCESSORS%"
)

if "%DO_BASH%"=="1" (
  echo Starting interactive shell in EF5 container...
  echo   /data   -^> %ROOT%\data
  echo   /output -^> %ROOT%\output
  echo   /conf   -^> %ROOT%\conf
  docker compose run --rm ef5 /bin/sh
  exit /b %ERRORLEVEL%
)

REM Strip optional conf\ or conf/ prefix
set "CTRL=%CONTROL%"
if /I "%CTRL:~0,5%"=="conf\" set "CTRL=%CTRL:~5%"
if /I "%CTRL:~0,5%"=="conf/" set "CTRL=%CTRL:~5%"

if not exist "%ROOT%\conf\%CTRL%" (
  echo ERROR: control file not found: %ROOT%\conf\%CTRL%
  echo   Must live inside conf\
  exit /b 1
)

echo ==============================================
echo   EF5 Docker - run (Windows CMD)
echo ==============================================
echo   Image   : %IMAGE%
echo   Control : %ROOT%\conf\%CTRL%
echo   Data    : %ROOT%\data    -^> /data
echo   Output  : %ROOT%\output  -^> /output
echo   Conf    : %ROOT%\conf    -^> /conf
echo   OMP     : %OMP_NUM_THREADS% threads
echo ==============================================

docker compose run --rm -e "OMP_NUM_THREADS=%OMP_NUM_THREADS%" ef5 /ef5/bin/ef5 "/conf/%CTRL%"
if errorlevel 1 exit /b 1

echo.
echo EF5 run finished. Results are in %ROOT%\output\
exit /b 0

:usage
echo Usage:
echo   run_ef5.cmd
echo   run_ef5.cmd -Control control_900m.txt
echo   run_ef5.cmd -Control control_90m_cuenca.txt
echo   run_ef5.cmd -Bash
exit /b 1
