#!/usr/bin/env bash
# ============================================================================
# run_ef5.sh — Run EF5 (Docker) for the EF5_GuatemalaTraining workspace
# ============================================================================
# The EF5 Docker container accesses the workspace folders via bind mounts:
#
#   ./data    -> /data    (model inputs: basic, parameters, pet, states, precip)
#   ./output  -> /output  (EF5 results: maxq/maxunitq/ts*.tif, timeseries csv)
#   ./conf    -> /conf    (EF5 control file, mounted read-only)
#
# Paths inside conf/control_900m.txt are relative to the container root (/), e.g.
#   DEM=data/basic/DEM_guatemala_900m.tif
#   OUTPUT=output/
#   STATES=data/states/
#
# Platforms:
#   Linux          -> optimized docker run (host networking, full resources)
#   macOS          -> docker compose (Docker Desktop has no host networking)
#   Windows        -> use run_ef5.cmd or `docker compose run --rm ef5`
#
# Usage:
#   ./run_ef5.sh                          # run with conf/control_900m.txt
#   ./run_ef5.sh conf/my_control.txt      # run with a different control file
#   ./run_ef5.sh --bash                   # interactive shell (inspect data)
#
# Requires image ef5-container:latest already present (build/load separately).
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_NAME="${EF5_IMAGE:-ef5-container:latest}"
CONTROL_FILE="${1:-conf/control_900m.txt}"

# --- Detect OS ---------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macos" ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows detected — use the CMD launcher instead:" >&2
        echo "    run_ef5.cmd -Control control_900m.txt" >&2
        echo "  or directly: docker compose run --rm ef5" >&2
        exit 1
        ;;
    *) PLATFORM="linux" ;;
esac

# --- Detect system resources ------------------------------------------------
if [[ "$PLATFORM" == "macos" ]]; then
    TOTAL_CPUS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
else
    TOTAL_CPUS=$(nproc)
fi
SHM_SIZE="32g"
NOFILE_LIMIT="1048576"

# --- Require image already present (no load / build) -------------------------
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "ERROR: Docker image ${IMAGE_NAME} not found." >&2
    echo "  Build it first:  ./docker/build_ef5.sh --rebuild" >&2
    echo "  Or load offline: ./docker/build_ef5.sh --load" >&2
    exit 1
fi

# --- Interactive shell mode ---------------------------------------------------
if [[ "${1:-}" == "--bash" ]] || [[ "${1:-}" == "-b" ]]; then
    if [[ "$PLATFORM" == "macos" ]]; then
        echo "Starting interactive shell in EF5 container (docker compose)..."
        exec docker compose run --rm ef5 /bin/sh
    fi
    echo "Starting interactive shell in EF5 container..."
    echo "  /data    -> ${SCRIPT_DIR}/data    (inputs)"
    echo "  /output  -> ${SCRIPT_DIR}/output  (results)"
    echo "  /conf    -> ${SCRIPT_DIR}/conf    (control files)"
    exec docker run -it --rm \
        --network host --ipc host \
        --shm-size="${SHM_SIZE}" \
        --ulimit nofile="${NOFILE_LIMIT}:${NOFILE_LIMIT}" \
        --security-opt seccomp=unconfined \
        -v "${SCRIPT_DIR}/data:/data:rw" \
        -v "${SCRIPT_DIR}/output:/output:rw" \
        -v "${SCRIPT_DIR}/conf:/conf:ro" \
        -u "$(id -u):$(id -g)" \
        -e OMP_NUM_THREADS="${TOTAL_CPUS}" \
        -e OMP_PROC_BIND=true \
        -e OMP_PLACES=cores \
        -w / \
        "${IMAGE_NAME}" \
        /bin/sh
fi

# --- Validate control file ----------------------------------------------------
# The control file must live inside ./conf so it is visible at /conf/<rel>.
CONF_DIR="${SCRIPT_DIR}/conf"
CONTROL_ABS="$(cd "$(dirname "$CONTROL_FILE")" && pwd)/$(basename "$CONTROL_FILE")"
if [[ ! -f "$CONTROL_ABS" ]]; then
    echo "ERROR: control file not found: ${CONTROL_ABS}" >&2
    exit 1
fi
case "$CONTROL_ABS" in
    "${CONF_DIR}"/*) ;;
    *)
        echo "ERROR: control file must be inside ${CONF_DIR}/" >&2
        echo "  Got: ${CONTROL_ABS}" >&2
        exit 1
        ;;
esac
CONTROL_IN_CONF="${CONTROL_ABS#"$CONF_DIR"/}"

echo "=============================================="
echo "  EF5 Docker — run (${PLATFORM})"
echo "=============================================="
echo "  Image   : ${IMAGE_NAME}"
echo "  Control : ${CONTROL_ABS}"
echo "  Data    : ${SCRIPT_DIR}/data   -> /data"
echo "  Output  : ${SCRIPT_DIR}/output -> /output"
echo "  Conf    : ${SCRIPT_DIR}/conf   -> /conf"
echo "  OMP     : ${TOTAL_CPUS} threads"
echo "=============================================="

# Docker Desktop (macOS) has no host networking — run via docker compose.
if [[ "$PLATFORM" == "macos" ]]; then
    docker compose run --rm \
        -e "OMP_NUM_THREADS=${TOTAL_CPUS}" \
        ef5 /ef5/bin/ef5 "/conf/${CONTROL_IN_CONF}"
    echo ""
    echo "EF5 run finished. Results are in ${SCRIPT_DIR}/output/"
    exit 0
fi

docker run --rm \
    --network host --ipc host \
    --shm-size="${SHM_SIZE}" \
    --ulimit nofile="${NOFILE_LIMIT}:${NOFILE_LIMIT}" \
    --ulimit nproc=65535:65535 \
    --ulimit memlock=-1:-1 \
    --security-opt seccomp=unconfined \
    -v "${SCRIPT_DIR}/data:/data:rw" \
    -v "${SCRIPT_DIR}/output:/output:rw" \
    -v "${SCRIPT_DIR}/conf:/conf:ro" \
    -u "$(id -u):$(id -g)" \
    -e OMP_NUM_THREADS="${TOTAL_CPUS}" \
    -e OMP_PROC_BIND=true \
    -e OMP_PLACES=cores \
    -w / \
    "${IMAGE_NAME}" \
    /ef5/bin/ef5 "/conf/${CONTROL_IN_CONF}"

echo ""
echo "EF5 run finished. Results are in ${SCRIPT_DIR}/output/"
