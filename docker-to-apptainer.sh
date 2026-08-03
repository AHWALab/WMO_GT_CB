#!/usr/bin/env bash
# ============================================================================
# Convert TITO Docker image → Apptainer/Singularity SIF
# ============================================================================
# EF5 is NOT converted here anymore. Apptainer partners run the glibc
# binary EF5/bin/ef5 inside the TITO SIF (EF5_RUNTIME=local) — no nest.
# Build that binary on a Docker host once:
#   ./EF5/docker/build_ef5_local.sh
#
# Typical flow:
#   [Docker host]  build tito:latest + ./EF5/docker/build_ef5_local.sh
#                  docker save tito → dist/docker-archives/tito_latest.tar.gz
#   [HPC]          ./docker-to-apptainer.sh     # → tito.sif only
#                  ensure EF5/bin/ef5 is present (copied from Docker host)
#                  TITO_RUNTIME=apptainer ./tito-run.sh operational --regions Guatemala
#
# Usage:
#   ./docker-to-apptainer.sh                # TITO only (default)
#   ./docker-to-apptainer.sh --from-daemon  # from local docker image
#   ./docker-to-apptainer.sh --with-ef5     # optional legacy EF5 SIF (not used by tito-run)
#
# Inputs (default):
#   dist/docker-archives/tito_latest.tar.gz
#
# Outputs:
#   ./tito.sif
#   (optional) ./EF5/ef5-container.sif  only with --with-ef5
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ARCHIVE_DIR="${ARCHIVE_DIR:-$SCRIPT_DIR/dist/docker-archives}"
TITO_ARCHIVE="${TITO_ARCHIVE:-$ARCHIVE_DIR/tito_latest.tar.gz}"
EF5_ARCHIVE="${EF5_ARCHIVE:-$ARCHIVE_DIR/ef5-container_latest.tar.gz}"
TITO_SIF="${TITO_SIF:-$SCRIPT_DIR/tito.sif}"
EF5_SIF="${EF5_SIF:-$SCRIPT_DIR/EF5/ef5-container.sif}"
EF5_LOCAL_BIN="${EF5_LOCAL_BIN:-$SCRIPT_DIR/EF5/bin/ef5}"
TMPDIR="${TMPDIR:-/tmp}"

DO_TITO=true
DO_EF5=false   # default OFF — Apptainer uses EF5/bin/ef5
FROM_DAEMON=false

for arg in "$@"; do
    case "$arg" in
        --tito-only) DO_EF5=false ;;
        --with-ef5|--ef5-only)
            # legacy: still allow building EF5 SIF if someone wants it
            if [[ "$arg" == "--ef5-only" ]]; then DO_TITO=false; fi
            DO_EF5=true
            ;;
        --from-daemon) FROM_DAEMON=true ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
    esac
done

if command -v apptainer >/dev/null 2>&1; then
    APPTAINER_CMD=apptainer
elif command -v singularity >/dev/null 2>&1; then
    APPTAINER_CMD=singularity
else
    echo "ERROR: neither apptainer nor singularity found in PATH."
    exit 1
fi

echo "=============================================="
echo "  Docker → Apptainer conversion"
echo "=============================================="
echo "  Tool     : $APPTAINER_CMD ($($APPTAINER_CMD --version 2>&1 | head -1))"
echo "  TITO SIF : $DO_TITO"
echo "  EF5 SIF  : $DO_EF5  (legacy; Apptainer run uses EF5/bin/ef5)"
echo "  Source   : $( $FROM_DAEMON && echo docker-daemon || echo docker-archive )"
echo "=============================================="

convert_archive() {
    local archive="$1"
    local out_sif="$2"
    local label="$3"

    if [[ ! -f "$archive" ]]; then
        echo "ERROR: missing archive for $label: $archive"
        exit 1
    fi

    mkdir -p "$(dirname "$out_sif")"
    local tar_path
    tar_path="$(mktemp "$TMPDIR/${label}.XXXXXX.tar")"

    echo ""
    echo ">>> [$label] gunzip $archive → $tar_path"
    gunzip -c "$archive" > "$tar_path"

    echo ">>> [$label] $APPTAINER_CMD build → $out_sif"
    echo "    (large images can take 10–30+ minutes)"
    "$APPTAINER_CMD" build --force "$out_sif" "docker-archive://$tar_path"
    rm -f "$tar_path"
    ls -lh "$out_sif"
}

convert_daemon() {
    local image="$1"
    local out_sif="$2"
    local label="$3"

    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: --from-daemon needs docker on this machine."
        exit 1
    fi
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        echo "ERROR: docker image not found: $image"
        exit 1
    fi

    mkdir -p "$(dirname "$out_sif")"
    echo ""
    echo ">>> [$label] $APPTAINER_CMD build from docker-daemon://$image"
    "$APPTAINER_CMD" build --force "$out_sif" "docker-daemon://$image"
    ls -lh "$out_sif"
}

if $DO_TITO; then
    if $FROM_DAEMON; then
        convert_daemon "tito:latest" "$TITO_SIF" "tito"
    else
        convert_archive "$TITO_ARCHIVE" "$TITO_SIF" "tito"
    fi
fi

if $DO_EF5; then
    echo ""
    echo "NOTE: EF5 SIF is optional/legacy. ./tito-run.sh (Apptainer) uses EF5/bin/ef5."
    if $FROM_DAEMON; then
        convert_daemon "ef5-container:latest" "$EF5_SIF" "ef5"
    else
        if [[ -f "$EF5_ARCHIVE" ]]; then
            convert_archive "$EF5_ARCHIVE" "$EF5_SIF" "ef5"
        elif [[ -f "$EF5_SIF" ]]; then
            echo ">>> [ef5] archive missing but SIF already exists — skipping"
            ls -lh "$EF5_SIF"
        else
            echo "WARNING: no EF5 archive — skip EF5 SIF."
        fi
    fi
fi

echo ""
echo "=============================================="
echo "  Conversion complete"
echo "=============================================="
[[ -f "$TITO_SIF" ]] && ls -lh "$TITO_SIF"
if [[ -x "$EF5_LOCAL_BIN" ]]; then
    ls -lh "$EF5_LOCAL_BIN"
else
    echo "WARNING: missing $EF5_LOCAL_BIN"
    echo "  On Docker host: ./EF5/docker/build_ef5_local.sh"
    echo "  Then copy EF5/bin/ef5 to this machine."
fi
[[ -f "$EF5_SIF" ]] && echo "(legacy SIF present, unused by Apptainer launcher:) $(ls -lh "$EF5_SIF")"
echo ""
echo "  Run with Apptainer:"
echo "    TITO_RUNTIME=apptainer ./tito-run.sh operational --regions Guatemala"
echo ""
