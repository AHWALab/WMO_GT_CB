@echo off
REM ============================================================================
REM build_ef5.cmd — pure Windows CMD (no PowerShell)
REM ============================================================================
REM Avoids PowerShell execution-policy / Group Policy blocks entirely.
REM
REM Usage (from repo root or docker\):
REM   docker\build_ef5.cmd
REM   docker\build_ef5.cmd -Status
REM   docker\build_ef5.cmd -Load
REM   docker\build_ef5.cmd -Rebuild
REM   docker\build_ef5.cmd -Rebuild -NoCache
REM   docker\build_ef5.cmd -Save
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "SCRIPT_DIR=%CD%"
if defined EF5_IMAGE (
  set "IMAGE=%EF5_IMAGE%"
) else (
  set "IMAGE=ef5-container:latest"
)
set "ARCHIVE=%SCRIPT_DIR%\ef5-container.tar"

set "DO_REBUILD=0"
set "DO_LOAD=0"
set "DO_SAVE=0"
set "DO_STATUS=0"
set "NO_CACHE="

:parse
if "%~1"=="" goto parsed
if /I "%~1"=="-Status"  set "DO_STATUS=1" & shift & goto parse
if /I "%~1"=="--status" set "DO_STATUS=1" & shift & goto parse
if /I "%~1"=="-Load"    set "DO_LOAD=1" & shift & goto parse
if /I "%~1"=="--load"   set "DO_LOAD=1" & shift & goto parse
if /I "%~1"=="-Rebuild" set "DO_REBUILD=1" & shift & goto parse
if /I "%~1"=="--rebuild" set "DO_REBUILD=1" & shift & goto parse
if /I "%~1"=="-NoCache" set "NO_CACHE=--no-cache" & shift & goto parse
if /I "%~1"=="--no-cache" set "NO_CACHE=--no-cache" & shift & goto parse
if /I "%~1"=="-Save"    set "DO_SAVE=1" & shift & goto parse
if /I "%~1"=="--save"   set "DO_SAVE=1" & shift & goto parse
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if /I "%~1"=="/?" goto usage
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

echo ==============================================
echo   EF5 Docker image - build / reuse (Windows CMD)
echo ==============================================
echo   Image   : %IMAGE%
echo   Archive : %ARCHIVE%
echo ==============================================

if "%DO_STATUS%"=="1" goto status
if "%DO_LOAD%"=="1" goto load
if "%DO_REBUILD%"=="1" goto rebuild

REM default: reuse / load / build
docker image inspect "%IMAGE%" >nul 2>&1
if not errorlevel 1 (
  echo ^>^>^> Reusing existing image %IMAGE%
  echo     For a fresh build: docker\build_ef5.cmd -Rebuild
  goto maybe_save
)
if exist "%ARCHIVE%" (
  echo ^>^>^> Image not loaded locally - loading prebuilt archive.
  goto do_load
)
echo ^>^>^> No image and no archive - building from Dockerfile.
goto do_build

:status
docker image inspect "%IMAGE%" >nul 2>&1
if not errorlevel 1 (
  echo   ef5-container image: PRESENT locally ^(%IMAGE%^)
  exit /b 0
)
if exist "%ARCHIVE%" (
  echo   ef5-container image: NOT loaded - archive present.
  echo   Load it with: docker\build_ef5.cmd -Load
  exit /b 0
)
echo   ef5-container image: NOT present, no archive. Rebuild with -Rebuild.
exit /b 0

:load
if not exist "%ARCHIVE%" (
  echo ERROR: archive not found: %ARCHIVE%
  echo   Build first: docker\build_ef5.cmd -Rebuild -Save
  exit /b 1
)
goto do_load

:do_load
echo ^>^>^> Loading image from %ARCHIVE%
docker load -i "%ARCHIVE%"
if errorlevel 1 exit /b 1
echo ^>^>^> Image loaded.
exit /b 0

:rebuild
echo ^>^>^> Building %IMAGE% from Dockerfile (compiles EF5 from source^)...
echo     Needs internet (clones AHWALab/EF5^) and takes a few minutes.
goto do_build

:do_build
docker build %NO_CACHE% -t "%IMAGE%" "%SCRIPT_DIR%"
if errorlevel 1 exit /b 1
echo ^>^>^> Build complete.
goto maybe_save

:maybe_save
if not "%DO_SAVE%"=="1" goto done
echo ^>^>^> Saving %IMAGE% -^> %ARCHIVE%
docker save "%IMAGE%" -o "%ARCHIVE%"
if errorlevel 1 exit /b 1
for %%A in ("%ARCHIVE%") do echo     %%~fA  %%~zA bytes
goto done

:done
echo.
echo   Done. Image: %IMAGE%
echo   Run EF5 with:  run_ef5.cmd -Control control_900m.txt
echo   Status check:  docker\build_ef5.cmd -Status
exit /b 0

:usage
echo Usage: build_ef5.cmd [-Status^|-Load^|-Rebuild^|-NoCache^|-Save]
exit /b 1
