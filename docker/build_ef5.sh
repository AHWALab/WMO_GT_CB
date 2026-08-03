#!/usr/bin/env bash
# ============================================================================
# build_ef5.sh — Build the EF5 Docker image OR reuse an existing one
# ============================================================================
# EF5_GuatemalaTraining standalone image management.
#
# Default behaviour (no flags): REUSE the already-installed image.
#   1. `ef5-container:latest` exists locally            -> reuse it
#   2. else if docker/ef5-container.tar is present        -> load it (offline)
#   3. else                                              -> build from source
#
# Flags:
#   --rebuild   force a fresh build from docker/Dockerfile (needs internet:
#               clones AHWALab/EF5 from GitHub and compiles)
#   --load      force loading the image from docker/ef5-container.tar
#   --no-cache  rebuild without Docker layer cache
#   --save      after ensuring the image exists, save it to
#               docker/ef5-container.tar (so it can be reused without compiling)
#   --status    print which image the run scripts will use
#
# Usage:
#   ./docker/build_ef5.sh                 # reuse existing / load / build
#   ./docker/build_ef5.sh --rebuild       # recompile from source
#   ./docker/build_ef5.sh --load          # load from prebuilt tar
#   ./docker/build_ef5.sh --save          # snapshot current image to tar
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_NAME="${EF5_IMAGE:-ef5-container:latest}"
ARCHIVE="${SCRIPT_DIR}/ef5-container.tar"

DO_REBUILD=false
DO_LOAD=false
DO_SAVE=false
DO_STATUS=false
NO_CACHE=""

for arg in "$@"; do
    case "$arg" in
        --rebuild) DO_REBUILD=true ;;
        --load) DO_LOAD=true ;;
        --no-cache) NO_CACHE="--no-cache" ;;
        --save) DO_SAVE=true ;;
        --status) DO_STATUS=true ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found in PATH" >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: cannot talk to the Docker daemon" >&2
    exit 1
fi

image_exists() {
    docker image inspect "$IMAGE_NAME" >/dev/null 2>&1
}

echo "=============================================="
echo "  EF5 Docker image — build / reuse"
echo "=============================================="
echo "  Image   : ${IMAGE_NAME}"
echo "  Archive : ${ARCHIVE}"
echo "=============================================="

# --status: just report what is available
if $DO_STATUS; then
    if image_exists; then
        echo "  ef5-container image: PRESENT locally (${IMAGE_NAME})"
    elif [[ -f "$ARCHIVE" ]]; then
        echo "  ef5-container image: NOT loaded — archive present at ${ARCHIVE}"
        echo "  Load it with: ./docker/build_ef5.sh --load"
    else
        echo "  ef5-container image: NOT present, no archive. Build with --rebuild."
    fi
    exit 0
fi

# --load: force load from archive
if $DO_LOAD; then
    if [[ ! -f "$ARCHIVE" ]]; then
        echo "ERROR: archive not found: ${ARCHIVE}" >&2
        echo "  Build the image first: ./docker/build_ef5.sh --rebuild --save" >&2
        exit 1
    fi
    echo ">>> Loading image from ${ARCHIVE}"
    docker load -i "$ARCHIVE"
    echo ">>> Image loaded."
    $DO_SAVE && { echo ">>> (nothing to save — image came from the archive)"; DO_SAVE=false; }
    exit 0
fi

# --rebuild: force compile from Dockerfile
if $DO_REBUILD; then
    echo ">>> Building ${IMAGE_NAME} from Dockerfile (compiles EF5 from source)..."
    echo "    This needs internet (clones AHWALab/EF5) and takes a few minutes."
    docker build $NO_CACHE -t "$IMAGE_NAME" .
    echo ">>> Build complete."
elif image_exists; then
    echo ">>> Reusing existing image ${IMAGE_NAME} (present locally)."
    echo "    For a fresh build: ./docker/build_ef5.sh --rebuild"
elif [[ -f "$ARCHIVE" ]]; then
    echo ">>> Image not loaded locally — loading prebuilt archive ${ARCHIVE}"
    docker load -i "$ARCHIVE"
    echo ">>> Image loaded from archive."
else
    echo ">>> No image and no archive found — building from Dockerfile."
    echo "    This needs internet (clones AHWALab/EF5) and takes a few minutes."
    docker build $NO_CACHE -t "$IMAGE_NAME" .
    echo ">>> Build complete."
fi

# --save: snapshot the image so it can be reused without compiling
if $DO_SAVE; then
    echo ">>> Saving ${IMAGE_NAME} -> ${ARCHIVE}"
    local_tmp="${ARCHIVE}.partial"
    docker save "$IMAGE_NAME" -o "$local_tmp"
    mv -f "$local_tmp" "$ARCHIVE"
    ls -lh "$ARCHIVE"
fi

echo ""
echo "=============================================="
echo "  Done. Image: ${IMAGE_NAME}"
echo "  Run EF5 with:  ../run_ef5.sh"
echo "  Status check:  ./build_ef5.sh --status"
echo "=============================================="
