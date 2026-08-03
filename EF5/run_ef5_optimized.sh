#!/usr/bin/env bash
# ============================================================================
# EF5 Docker Run Script — OPTIMIZED for full computational power
# ============================================================================
# This script runs the EF5 Docker container with settings that give it
# near-native performance, utilizing ALL available CPU cores, RAM, and I/O.
#
# The entire TITO_Stream_Sat/ parent directory is mounted as /data inside the
# container, giving EF5 read/write access to ALL subdirectories:
#   basic/  parameters/  pet/  precip/  precipEF5/  states/  outputs/  logs/  ...
#
# Control files live in:  /data/templates/  (TITO_Stream_Sat/templates/)
#
# Usage:
#   ./run_ef5_optimized.sh                                    # uses default template
#   ./run_ef5_optimized.sh ef5_Antigua_control_template.txt   # specific template
#   ./run_ef5_optimized.sh --bash                             # interactive shell
#
# Inside the container, your files are at:
#   /data/templates/     → EF5 control file templates
#   /data/basic/         → DEM, DDM, FAM rasters
#   /data/parameters/    → parameter grids
#   /data/pet/           → PET forcing data
#   /data/precip/        → precipitation forcing
#   /data/precipEF5/     → EF5-formatted precipitation
#   /data/states/        → model states (read/write)
#   /data/outputs/       → model outputs (read/write)
# ============================================================================

set -e

# --- Configuration -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# PARENT = TITO_Stream_Sat/ — the master data directory mounted into the container
PARENT_DIR="$(dirname "${SCRIPT_DIR}")"
IMAGE_NAME="ef5-container:latest"
# Default control file: relative to /data/ (i.e., TITO_Stream_Sat/)
CONTROL_FILE="${1:-templates/ef5_Antigua_control_template.txt}"

# --- Detect system resources ------------------------------------------------
TOTAL_CPUS=$(nproc)
TOTAL_MEM_GB=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
SHM_SIZE="32g"          # Shared memory for /dev/shm (important for parallel I/O)
NOFILE_LIMIT="1048576"  # Max open file descriptors

echo "=============================================="
echo "  EF5 Docker — Optimized Run"
echo "=============================================="
echo "  System:     ${TOTAL_CPUS} CPUs, ${TOTAL_MEM_GB} GB RAM"
echo "  Image:      ${IMAGE_NAME}"
echo "  Control:    /data/${CONTROL_FILE}"
echo "  Data mount: ${PARENT_DIR} → /data (rw)"
echo "  Outputs:    ${PARENT_DIR}/outputs/"
echo "=============================================="

# --- Interactive shell mode --------------------------------------------------
if [[ "$1" == "--bash" ]] || [[ "$1" == "-b" ]]; then
    echo "Starting interactive bash shell in container..."
    echo "Your data is at /data/ (templates, basic, parameters, pet, precip, states, outputs, etc.)"
    docker run -it --rm \
        --network host \
        --ipc host \
        --shm-size="${SHM_SIZE}" \
        --ulimit nofile="${NOFILE_LIMIT}:${NOFILE_LIMIT}" \
        --ulimit nproc=65535:65535 \
        --ulimit memlock=-1:-1 \
        --security-opt seccomp=unconfined \
        -v "${PARENT_DIR}:/data:rw" \
        -u "$(id -u):$(id -g)" \
        -e OMP_NUM_THREADS="${TOTAL_CPUS}" \
        -e OMP_PROC_BIND=true \
        -e OMP_PLACES=cores \
        -w /data \
        "${IMAGE_NAME}" \
        /bin/sh
    exit 0
fi

# --- Run EF5 model -----------------------------------------------------------
echo ""
echo "Running EF5 with control file: /data/${CONTROL_FILE}"
echo "OpenMP threads: ${TOTAL_CPUS}"
echo ""

docker run -it --rm \
    --network host \
    --ipc host \
    --shm-size="${SHM_SIZE}" \
    --ulimit nofile="${NOFILE_LIMIT}:${NOFILE_LIMIT}" \
    --ulimit nproc=65535:65535 \
    --ulimit memlock=-1:-1 \
    --security-opt seccomp=unconfined \
    -v "${PARENT_DIR}:/data:rw" \
    -u "$(id -u):$(id -g)" \
    -e OMP_NUM_THREADS="${TOTAL_CPUS}" \
    -e OMP_PROC_BIND=true \
    -e OMP_PLACES=cores \
    -w /data \
    "${IMAGE_NAME}" \
    /ef5/bin/ef5 "/data/${CONTROL_FILE}"

echo ""
echo "EF5 run completed. Check ${PARENT_DIR}/outputs/ for results."
