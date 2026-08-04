# ===============================ESPAÑOL=============================================
# run_ef5.ps1 — Ejecutar EF5 (Docker) en Windows
# ============================================================================
# Utiliza Docker Compose (Docker Desktop) para que los montajes (bind mounts)
# de las carpetas de datos, salida y configuración se resuelvan
# automáticamente en Windows.
#
# Uso:
#   .\run_ef5.ps1                         # ejecuta conf/control.txt
#   .\run_ef5.ps1 -Control my_control.txt # ejecuta conf/my_control.txt
#   .\run_ef5.ps1 -Bash                   # abre una consola interactiva
#
# La imagen se garantiza mediante docker/build_ef5.ps1 (reutilizar / cargar / compilar).
# ============================================================================

# ==============================ENGLISH==============================================
# run_ef5.ps1 — Run EF5 (Docker) on Windows
# ============================================================================
# Uses docker compose (Docker Desktop) so the data/output/conf bind mounts
# resolve automatically on Windows.
#
# Usage:
#   .\run_ef5.ps1                         # run conf/control.txt
#   .\run_ef5.ps1 -Control my_control.txt # run conf/my_control.txt
#   .\run_ef5.ps1 -Bash                   # interactive shell
#
# The image is ensured via docker/build_ef5.ps1 (reuse / load / build).
# ============================================================================
param(
    [string]$Control = "control.txt",
    [switch]$Bash
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$Image = if ($env:EF5_IMAGE) { $env:EF5_IMAGE } else { "ef5-container:latest" }
$ConfDir = Join-Path $ScriptDir "conf"

# --- Make sure Docker is available -----------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker not found in PATH. Install Docker Desktop first."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cannot talk to the Docker daemon. Is Docker Desktop running?"
}

# --- Make sure the image is available (reuse / load / build) -----------------
$haveImage = $false
docker image inspect $Image *> $null
if ($LASTEXITCODE -eq 0) { $haveImage = $true }
if (-not $haveImage) {
    Write-Host "Image $Image not found - preparing it..."
    & (Join-Path $ScriptDir "docker\build_ef5.ps1")
}

# --- Interactive shell --------------------------------------------------------
if ($Bash) {
    Write-Host "Starting interactive shell in EF5 container..."
    Write-Host "  /data   -> $ScriptDir\data"
    Write-Host "  /output -> $ScriptDir\output"
    Write-Host "  /conf   -> $ScriptDir\conf"
    & docker compose run --rm ef5 /bin/sh
    exit $LASTEXITCODE
}

# --- Validate control file ----------------------------------------------------
$ControlAbs = Join-Path $ConfDir $Control
if (-not (Test-Path $ControlAbs)) {
    Write-Error "Control file not found: $ControlAbs (must be inside conf/)"
}
if (-not $ControlAbs.StartsWith($ConfDir + [IO.Path]::DirectorySeparatorChar)) {
    Write-Error "Control file must be inside conf/: $ControlAbs"
}
$ControlRel = $ControlAbs.Substring($ConfDir.Length + 1).Replace("\", "/")

# --- Threads (default: all CPUs) ---------------------------------------------
if (-not $env:OMP_NUM_THREADS) {
    $env:OMP_NUM_THREADS = "$([Environment]::ProcessorCount)"
}

Write-Host "=============================================="
Write-Host "  EF5 Docker - run (Windows)"
Write-Host "=============================================="
Write-Host "  Image   : $Image"
Write-Host "  Control : $ControlAbs"
Write-Host "  Data    : $ScriptDir\data    -> /data"
Write-Host "  Output  : $ScriptDir\output  -> /output"
Write-Host "  Conf    : $ScriptDir\conf    -> /conf"
Write-Host "  OMP     : $env:OMP_NUM_THREADS threads"
Write-Host "=============================================="

& docker compose run --rm `
    -e "OMP_NUM_THREADS=$env:OMP_NUM_THREADS" `
    ef5 /ef5/bin/ef5 "/conf/$ControlRel"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "EF5 run finished. Results are in $ScriptDir\output\"
