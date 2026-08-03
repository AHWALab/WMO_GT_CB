#!/usr/bin/env bash
# ============================================================================
# TITO unified launcher — Docker OR Apptainer/Singularity OR native conda
# ============================================================================
# Partner product rule:
#   • Docker partners  → TITO docker image + EF5 via docker.sock (sibling)
#   • Apptainer partners → TITO SIF + EF5 as LOCAL glibc binary (NO nesting)
#
# Nested Apptainer→Apptainer is intentionally NOT used (HPC setuid/session
# failures). Build the local binary once on a Docker host:
#   ./EF5/docker/build_ef5_local.sh    # → EF5/bin/ef5
#
# ── Operational ─────────────────────────────────────────────────────────────
#   TITO_RUNTIME=docker     ./tito-run.sh operational --regions Guatemala
#   TITO_RUNTIME=apptainer  ./tito-run.sh operational --regions Guatemala
#
# ── Hindcast ────────────────────────────────────────────────────────────────
#   TITO_RUNTIME=apptainer ./tito-run.sh hindcast \
#       "2026-07-22 00:00" "2026-07-22 06:00" --regions Guatemala
#
# Env knobs:
#   TITO_RUNTIME   docker | apptainer | singularity | native
#   TITO_IMAGE     Docker image (default: tito:latest)
#   TITO_SIF       Apptainer SIF (default: ./tito.sif)
#   EF5_LOCAL_BIN  glibc EF5 path (default: EF5/bin/ef5)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TITO_IMAGE="${TITO_IMAGE:-tito:latest}"
TITO_SIF="${TITO_SIF:-$SCRIPT_DIR/tito.sif}"
EF5_SIF="${EF5_SIF:-$SCRIPT_DIR/EF5/ef5-container.sif}"
EF5_DOCKER_IMAGE="${EF5_DOCKER_IMAGE:-ef5-container:latest}"
EF5_LOCAL_BIN="${EF5_LOCAL_BIN:-$SCRIPT_DIR/EF5/bin/ef5}"

# ── Detect runtime ─────────────────────────────────────────────────────────
detect_runtime() {
    local forced="${TITO_RUNTIME:-}"
    forced="$(echo "$forced" | tr '[:upper:]' '[:lower:]')"
    if [[ "$forced" =~ ^(docker|apptainer|singularity|native)$ ]]; then
        echo "$forced"
        return
    fi

    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        if docker image inspect "$TITO_IMAGE" >/dev/null 2>&1; then
            echo "docker"
            return
        fi
    fi

    for cmd in apptainer singularity; do
        if command -v "$cmd" >/dev/null 2>&1 && [[ -f "$TITO_SIF" ]]; then
            echo "$cmd"
            return
        fi
    done

    if command -v conda >/dev/null 2>&1 || [[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]]; then
        echo "native"
        return
    fi

    echo "ERROR: No container runtime ready for partners." >&2
    echo "  Docker     : need tito:latest" >&2
    echo "  Apptainer  : ./docker-to-apptainer.sh then ./tito-run.sh" >&2
    exit 1
}

RUNTIME="$(detect_runtime)"
echo "==== TITO launcher ===="
echo "  Runtime : $RUNTIME"
echo "  Project : $SCRIPT_DIR"

DATA_MOUNTS=(
    states outputs precip precipEF5 qpf_store pet basic parameters templates
)

# ── Docker ─────────────────────────────────────────────────────────────────
run_docker() {
    local args=()
    for d in "${DATA_MOUNTS[@]}"; do
        mkdir -p "$SCRIPT_DIR/$d"
        args+=(-v "$SCRIPT_DIR/$d:/app/$d")
    done
    args+=(
        -v "$SCRIPT_DIR/Caribbean_Comoros_config.py:/app/Caribbean_Comoros_config.py:ro"
        -v "$SCRIPT_DIR/orchestrator.py:/app/orchestrator.py:ro"
        -v "$SCRIPT_DIR/hindcast_manager.py:/app/hindcast_manager.py:ro"
        -v "$SCRIPT_DIR/tito_utils:/app/tito_utils:ro"
        -v "$SCRIPT_DIR/STREAM-Sat-realtime:/app/STREAM-Sat-realtime:rw"
        -v "$SCRIPT_DIR/StormLab-GFS-realtime:/app/StormLab-GFS-realtime:rw"
        -v "$SCRIPT_DIR/EF5:/app/EF5:ro"
        -v /var/run/docker.sock:/var/run/docker.sock
        -e EF5_RUNTIME=docker
        -e "EF5_IMAGE=$EF5_DOCKER_IMAGE"
        -e "TITO_HOST_PROJECT=$SCRIPT_DIR"
        -e PYTHONUNBUFFERED=1
        -e TZ=Etc/UTC
        -e STORMLAB_USE_TITO_ENV=1
        --network host
        --rm
    )
    echo "  Image   : $TITO_IMAGE"
    echo "  EF5     : docker /$EF5_DOCKER_IMAGE (sibling via docker.sock)"
    echo "  Host    : $SCRIPT_DIR"
    exec docker run "${args[@]}" "$TITO_IMAGE" "$@"
}

# ── Apptainer / Singularity — TITO SIF + local EF5 binary (no nesting) ─────
run_apptainer() {
    local cmd="$1"; shift
    if [[ ! -f "$TITO_SIF" ]]; then
        echo "ERROR: TITO SIF not found: $TITO_SIF" >&2
        echo "  ./docker-to-apptainer.sh" >&2
        exit 1
    fi
    if [[ ! -x "$EF5_LOCAL_BIN" ]]; then
        echo "ERROR: glibc EF5 binary missing: $EF5_LOCAL_BIN" >&2
        echo "  Build once on a Docker host (no TITO rebuild needed):" >&2
        echo "    ./EF5/docker/build_ef5_local.sh" >&2
        echo "  Then re-run this Apptainer command." >&2
        exit 1
    fi

    local binds=()
    for d in "${DATA_MOUNTS[@]}"; do
        mkdir -p "$SCRIPT_DIR/$d"
        binds+=(--bind "$SCRIPT_DIR/$d:/app/$d")
    done
    binds+=(
        --bind "$SCRIPT_DIR/Caribbean_Comoros_config.py:/app/Caribbean_Comoros_config.py:ro"
        --bind "$SCRIPT_DIR/orchestrator.py:/app/orchestrator.py:ro"
        --bind "$SCRIPT_DIR/hindcast_manager.py:/app/hindcast_manager.py:ro"
        --bind "$SCRIPT_DIR/tito_utils:/app/tito_utils:ro"
        --bind "$SCRIPT_DIR/STREAM-Sat-realtime:/app/STREAM-Sat-realtime"
        --bind "$SCRIPT_DIR/StormLab-GFS-realtime:/app/StormLab-GFS-realtime"
        --bind "$SCRIPT_DIR/EF5:/app/EF5:ro"
    )

    echo "  SIF     : $TITO_SIF"
    echo "  EF5     : local /$EF5_LOCAL_BIN  (in-process, no nested Apptainer)"

    # EF5_RUNTIME=local → run_EF5 execs EF5/bin/ef5 inside this same container
    # StormLab uses the same tito_env2 python (sys.executable)
    exec "$cmd" run --cleanenv \
        --env "EF5_RUNTIME=local,EF5_LOCAL_BIN=/app/EF5/bin/ef5,PYTHONUNBUFFERED=1,TZ=Etc/UTC,STORMLAB_USE_TITO_ENV=1" \
        "${binds[@]}" \
        --pwd /app \
        "$TITO_SIF" "$@"
}

# ── Native conda (dev only) ────────────────────────────────────────────────
run_native() {
    echo "  Mode    : native conda"
    # shellcheck disable=SC1091
    if [[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]]; then
        source "$HOME/miniconda3/etc/profile.d/conda.sh"
    elif [[ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]]; then
        source "$HOME/anaconda3/etc/profile.d/conda.sh"
    else
        echo "ERROR: conda not found" >&2
        exit 1
    fi
    conda activate tito_env2
    unset EF5_RUNTIME || true
    export PYTHONUNBUFFERED=1

    if [[ -x "$SCRIPT_DIR/docker-entrypoint.sh" ]]; then
        exec bash "$SCRIPT_DIR/docker-entrypoint.sh" "$@"
    fi

    local mode="${1:-operational}"; shift || true
    case "$mode" in
        operational) exec python orchestrator.py Caribbean_Comoros_config.py "$@" ;;
        hindcast)
            local s="${1:?}"; local e="${2:?}"; shift 2
            exec python hindcast_manager.py Caribbean_Comoros_config.py "$s" "$e" "$@"
            ;;
        shell|bash) exec bash ;;
        *) echo "Unknown mode: $mode"; exit 1 ;;
    esac
}

case "$RUNTIME" in
    docker)                run_docker "$@" ;;
    apptainer|singularity) run_apptainer "$RUNTIME" "$@" ;;
    native)                run_native "$@" ;;
    *) echo "Unknown runtime: $RUNTIME"; exit 1 ;;
esac
