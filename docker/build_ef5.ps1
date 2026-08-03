# ============================================================================
# build_ef5.ps1 — Build the EF5 Docker image OR reuse an existing one (Windows)
# ============================================================================
# Mirrors docker/build_ef5.sh for PowerShell / Docker Desktop.
#
# Default behaviour (no switches): REUSE the already-installed image.
#   1. ef5-container:latest exists locally     -> reuse it
#   2. else if docker\ef5-container.tar exists  -> load it (offline)
#   3. else                                    -> build from source
#
# Switches:
#   -Rebuild   force a fresh build from Dockerfile (clones AHWALab/EF5)
#   -Load      force loading the image from docker\ef5-container.tar
#   -NoCache   rebuild without Docker layer cache
#   -Save      snapshot the current image -> docker\ef5-container.tar
#   -Status    print which image the run scripts will use
# ============================================================================
param(
    [switch]$Rebuild,
    [switch]$Load,
    [switch]$NoCache,
    [switch]$Save,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Image = if ($env:EF5_IMAGE) { $env:EF5_IMAGE } else { "ef5-container:latest" }
$Archive = Join-Path $ScriptDir "ef5-container.tar"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker not found in PATH. Install Docker Desktop first."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cannot talk to the Docker daemon. Is Docker Desktop running?"
}

function Test-Image {
    docker image inspect $Image *> $null
    return ($LASTEXITCODE -eq 0)
}

Write-Host "=============================================="
Write-Host "  EF5 Docker image - build / reuse (Windows)"
Write-Host "=============================================="
Write-Host "  Image   : $Image"
Write-Host "  Archive : $Archive"
Write-Host "=============================================="

if ($Status) {
    if (Test-Image) {
        Write-Host "  ef5-container image: PRESENT locally ($Image)"
    } elseif (Test-Path $Archive) {
        Write-Host "  ef5-container image: NOT loaded - archive present."
        Write-Host "  Load it with: .\docker\build_ef5.ps1 -Load"
    } else {
        Write-Host "  ef5-container image: NOT present, no archive. Rebuild with -Rebuild."
    }
    exit 0
}

if ($Load) {
    if (-not (Test-Path $Archive)) {
        Write-Error "Archive not found: $Archive (build first: build_ef5.ps1 -Rebuild -Save)"
    }
    Write-Host ">>> Loading image from $Archive"
    docker load -i $Archive
    Write-Host ">>> Image loaded."
    exit $LASTEXITCODE
}

function Invoke-Build {
    $dockerArgs = @("build")
    if ($NoCache) { $dockerArgs += "--no-cache" }
    $dockerArgs += @("-t", $Image, $ScriptDir)
    & docker @dockerArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Rebuild) {
    Write-Host ">>> Building $Image from Dockerfile (compiles EF5 from source)..."
    Write-Host "    Needs internet (clones AHWALab/EF5) and takes a few minutes."
    Invoke-Build
    Write-Host ">>> Build complete."
} elseif (Test-Image) {
    Write-Host ">>> Reusing existing image $Image (present locally)."
    Write-Host "    For a fresh build: .\docker\build_ef5.ps1 -Rebuild"
} elseif (Test-Path $Archive) {
    Write-Host ">>> Image not loaded locally - loading prebuilt archive."
    docker load -i $Archive
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host ">>> Image loaded from archive."
} else {
    Write-Host ">>> No image and no archive - building from Dockerfile."
    Write-Host "    Needs internet (clones AHWALab/EF5) and takes a few minutes."
    Invoke-Build
    Write-Host ">>> Build complete."
}

if ($Save) {
    Write-Host ">>> Saving $Image -> $Archive"
    docker save $Image -o $Archive
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Get-Item $Archive | Select-Object FullName, Length
}

Write-Host ""
Write-Host "  Done. Image: $Image"
Write-Host "  Run EF5 with:  ..\run_ef5.ps1"
Write-Host "  Status check:  .\docker\build_ef5.ps1 -Status"
