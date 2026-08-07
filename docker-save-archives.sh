#!/usr/bin/env bash
# ============================================================================
# Save local Docker images → gzipped archives under dist/docker-archives/
# ============================================================================
# Use after ./container-build.sh so partners / HPC / Zenodo can load images
# without rebuilding:
#
#   gunzip -c dist/docker-archives/tito_latest.tar.gz | docker load
#   gunzip -c dist/docker-archives/ef5-container_latest.tar.gz | docker load
#
# Usage:
#   ./docker-save-archives.sh              # save tito + ef5-container
#   ./docker-save-archives.sh --tito-only
#   ./docker-save-archives.sh --ef5-only
#
# Env:
#   ARCHIVE_DIR   output dir (default: dist/docker-archives)
#   TITO_IMAGE    default tito:latest
#   EF5_IMAGE     default ef5-container:latest
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ARCHIVE_DIR="${ARCHIVE_DIR:-$SCRIPT_DIR/dist/docker-archives}"
TITO_IMAGE="${TITO_IMAGE:-tito:latest}"
EF5_IMAGE="${EF5_IMAGE:-ef5-container:latest}"

DO_TITO=true
DO_EF5=true

for arg in "$@"; do
    case "$arg" in
        --tito-only) DO_EF5=false ;;
        --ef5-only) DO_TITO=false ;;
        -h|--help)
            sed -n '2,25p' "$0"
            exit 0
            ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found in PATH"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: cannot talk to Docker daemon"
    exit 1
fi

mkdir -p "$ARCHIVE_DIR"

save_one() {
    local image="$1"
    local out="$2"
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        echo "ERROR: image not found: $image"
        echo "  Build first: ./container-build.sh"
        exit 1
    fi
    echo ""
    echo ">>> Saving $image"
    echo "    → $out"
    echo "    (slim tito image — save can still take a few minutes)"
    local tmp="${out}.partial"
    docker save "$image" | gzip -c > "$tmp"
    mv -f "$tmp" "$out"
    ls -lh "$out"
}

echo "=============================================="
echo "  Docker → gzipped archives"
echo "=============================================="
echo "  Out dir : $ARCHIVE_DIR"
echo "  TITO    : $DO_TITO ($TITO_IMAGE)"
echo "  EF5     : $DO_EF5 ($EF5_IMAGE)"
echo "=============================================="

if $DO_TITO; then
    save_one "$TITO_IMAGE" "$ARCHIVE_DIR/tito_latest.tar.gz"
fi
if $DO_EF5; then
    save_one "$EF5_IMAGE" "$ARCHIVE_DIR/ef5-container_latest.tar.gz"
fi

echo ""
echo "=============================================="
echo "  Done"
echo "=============================================="
ls -lh "$ARCHIVE_DIR"/*.tar.gz 2>/dev/null || true
echo ""
echo "  Load later:"
echo "    gunzip -c $ARCHIVE_DIR/tito_latest.tar.gz | docker load"
echo "    gunzip -c $ARCHIVE_DIR/ef5-container_latest.tar.gz | docker load"
echo ""
echo "  For Apptainer on HPC:"
echo "    copy archives + EF5/bin/ef5, then ./docker-to-apptainer.sh"
echo ""
