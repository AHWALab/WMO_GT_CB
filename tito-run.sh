#!/usr/bin/env bash
# ============================================================================
# TITO unified launcher — Docker (Linux / macOS / Windows) OR Apptainer OR native
# ============================================================================
# Partner product rule:
#   • Docker partners  → TITO docker image + EF5 via docker.sock (sibling)
#   • Apptainer partners → TITO SIF + EF5 as LOCAL glibc binary (NO nesting)
#
# ── Load images from USB / dist (no rebuild) ────────────────────────────────
#   Place on the pendrive / repo:
#     dist/docker-archives/tito_latest.tar.gz
#     dist/docker-archives/ef5-container_latest.tar.gz
#   (also accepts dist/docker-images/ or plain dist/)
#   Then:
#     ./tito-run.sh load-images          # once
#     ./tito-run.sh operational --regions Guatemala
#
# ── Operational ─────────────────────────────────────────────────────────────
#   ./tito-run.sh operational --regions Guatemala
#   TITO_RUNTIME=docker ./tito-run.sh operational
#
# ── Hindcast ────────────────────────────────────────────────────────────────
#   ./tito-run.sh hindcast "2026-07-22 00:00" "2026-07-22 06:00" --regions Guatemala
#   Training offline (no downloads — uses offline_precips/ or staged EF5_conf precip):
#   ./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala --offline
#
# ── Windows ─────────────────────────────────────────────────────────────────
#   Prefer pure CMD (no PowerShell / execution-policy issues):
#     tito-run.cmd load-images
#     tito-run.cmd operational --regions Guatemala
#   Or Git Bash / WSL: ./tito-run.sh …
#
# Env knobs:
#   TITO_RUNTIME   docker | apptainer | singularity | native
#   TITO_IMAGE     Docker image (default: tito:latest)
#   EF5_DOCKER_IMAGE  default ef5-container:latest
#   TITO_SIF       Apptainer SIF (default: ./tito.sif)
#   EF5_LOCAL_BIN  glibc EF5 path (default: EF5/bin/ef5)
#   TITO_SKIP_LOAD=1  do not auto-load missing images from dist/
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TITO_IMAGE="${TITO_IMAGE:-tito:latest}"
TITO_SIF="${TITO_SIF:-$SCRIPT_DIR/tito.sif}"
EF5_SIF="${EF5_SIF:-$SCRIPT_DIR/EF5/ef5-container.sif}"
EF5_DOCKER_IMAGE="${EF5_DOCKER_IMAGE:-ef5-container:latest}"
EF5_LOCAL_BIN="${EF5_LOCAL_BIN:-$SCRIPT_DIR/EF5/bin/ef5}"

# ── OS detection ───────────────────────────────────────────────────────────
UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
case "$UNAME_S" in
    Linux*)   HOST_OS=linux ;;
    Darwin*)  HOST_OS=macos ;;
    MINGW*|MSYS*|CYGWIN*) HOST_OS=windows ;;
    *)        HOST_OS=other ;;
esac

# macOS: Gatekeeper quarantine on USB/download copies → "./script: Permission denied"
# USB/FAT/zip often strips +x — restore launchers when the FS allows it.
if [[ "$HOST_OS" == "macos" ]]; then
    xattr -dr com.apple.quarantine "$SCRIPT_DIR" 2>/dev/null || true
fi
chmod +x "$SCRIPT_DIR/tito-run.sh" "$SCRIPT_DIR/load-docker-images.sh" 2>/dev/null || true

# Host path Docker Desktop can bind-mount (critical on Windows Git Bash)
host_project_path() {
    local p="$SCRIPT_DIR"
    if [[ "$HOST_OS" == "windows" ]]; then
        if command -v cygpath >/dev/null 2>&1; then
            cygpath -m "$p"
            return
        fi
        # Git Bash: /c/Users/... → C:/Users/...
        if [[ "$p" =~ ^/([a-zA-Z])/(.*)$ ]]; then
            echo "${BASH_REMATCH[1]^}:/${BASH_REMATCH[2]}"
            return
        fi
    fi
    echo "$p"
}

HOST_PROJECT="$(host_project_path)"

# ── Find pre-built image archives under dist/ ──────────────────────────────
find_archive() {
    # $1 = basename without path, e.g. tito_latest.tar.gz
    local name="$1"
    local d
    for d in \
        "$SCRIPT_DIR/dist/docker-archives" \
        "$SCRIPT_DIR/dist/docker-images" \
        "$SCRIPT_DIR/dist" \
        "$SCRIPT_DIR"
    do
        if [[ -f "$d/$name" ]]; then
            echo "$d/$name"
            return 0
        fi
        # also accept uncompressed .tar
        local base="${name%.tar.gz}"
        if [[ -f "$d/${base}.tar" ]]; then
            echo "$d/${base}.tar"
            return 0
        fi
    done
    return 1
}

docker_image_present() {
    docker image inspect "$1" >/dev/null 2>&1
}

load_one_archive() {
    local archive="$1"
    local label="$2"
    echo "    Loading $label from:"
    echo "      $archive"
    if [[ ! -r "$archive" ]]; then
        echo "ERROR: cannot read archive (Permission denied or missing): $archive" >&2
        echo "  On macOS: copy off the USB to a local folder, or: xattr -dr com.apple.quarantine ." >&2
        return 1
    fi
    # Prefer gunzip|docker load — more reliable on Docker Desktop (macOS) than load -i .gz
    if [[ "$archive" == *.tar.gz ]] || [[ "$archive" == *.tgz ]]; then
        if command -v gunzip >/dev/null 2>&1; then
            if gunzip -c "$archive" | docker load; then
                return 0
            fi
        fi
        if command -v gzip >/dev/null 2>&1; then
            if gzip -dc "$archive" | docker load; then
                return 0
            fi
        fi
        # Last resort: Docker may accept .tar.gz directly
        if docker load -i "$archive"; then
            return 0
        fi
        echo "ERROR: failed to load $archive" >&2
        return 1
    fi
    docker load -i "$archive"
}

# Load tito + ef5 images from dist/ if missing (or when forced)
ensure_docker_images() {
    local force="${1:-0}"
    if [[ "${TITO_SKIP_LOAD:-0}" == "1" ]] && [[ "$force" != "1" ]]; then
        return 0
    fi
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    local _dinfo
    if ! _dinfo="$(docker info 2>&1)"; then
        if echo "$_dinfo" | grep -qiE 'permission denied|connect:|Cannot connect|Is the docker daemon'; then
            echo "ERROR: cannot talk to Docker daemon." >&2
            echo "  macOS/Windows: start Docker Desktop and wait until it is Running." >&2
            echo "  Linux: sudo systemctl start docker  (or add user to docker group)" >&2
            echo "  Detail: $_dinfo" >&2
        else
            echo "ERROR: Docker is installed but the daemon is not running." >&2
            echo "  Start Docker Desktop (Mac/Windows) or: sudo systemctl start docker (Linux)" >&2
            echo "  Detail: $_dinfo" >&2
        fi
        exit 1
    fi

    local need_tito=0 need_ef5=0
    docker_image_present "$TITO_IMAGE" || need_tito=1
    docker_image_present "$EF5_DOCKER_IMAGE" || need_ef5=1
    if [[ "$force" == "1" ]]; then
        need_tito=1
        need_ef5=1
    fi
    if [[ "$need_tito" -eq 0 && "$need_ef5" -eq 0 ]]; then
        return 0
    fi

    echo "==== Loading Docker images from dist/ (USB / pre-built) ===="
    if [[ "$need_tito" -eq 1 ]]; then
        local a
        if a="$(find_archive tito_latest.tar.gz)"; then
            load_one_archive "$a" "$TITO_IMAGE"
        else
            echo "ERROR: image '$TITO_IMAGE' not loaded and archive not found." >&2
            echo "  Place  dist/docker-archives/tito_latest.tar.gz  then re-run:" >&2
            echo "    ./tito-run.sh load-images" >&2
            exit 1
        fi
    fi
    if [[ "$need_ef5" -eq 1 ]]; then
        local a
        if a="$(find_archive ef5-container_latest.tar.gz)"; then
            load_one_archive "$a" "$EF5_DOCKER_IMAGE"
        else
            echo "ERROR: image '$EF5_DOCKER_IMAGE' not loaded and archive not found." >&2
            echo "  Place  dist/docker-archives/ef5-container_latest.tar.gz  then:" >&2
            echo "    ./tito-run.sh load-images" >&2
            exit 1
        fi
    fi
    echo "    Images ready."
}

cmd_load_images() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: docker not found in PATH" >&2
        exit 1
    fi
    ensure_docker_images 1
    echo ""
    echo "Loaded images:"
    docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" \
        | grep -E "REPOSITORY|tito|ef5-container" || true
}

# ── Detect runtime ─────────────────────────────────────────────────────────
detect_runtime() {
    local forced="${TITO_RUNTIME:-}"
    forced="$(echo "$forced" | tr '[:upper:]' '[:lower:]')"
    if [[ "$forced" =~ ^(docker|apptainer|singularity|native)$ ]]; then
        echo "$forced"
        return
    fi

    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        # Prefer docker if daemon is up (images may still need load)
        echo "docker"
        return
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

    echo "ERROR: No container runtime ready." >&2
    echo "  Docker (all platforms): install Docker Desktop / Engine, then:" >&2
    echo "    ./tito-run.sh load-images" >&2
    echo "    ./tito-run.sh operational --regions Guatemala" >&2
    echo "  Apptainer (Linux HPC): need tito.sif + EF5/bin/ef5" >&2
    exit 1
}

# EF5_conf holds basic/parameters/pet/templates/states/precip/precipEF5/qpf_store
DATA_MOUNTS=(
    EF5_conf outputs fim_config fim_store offline_precips offline
)

# ── Docker ─────────────────────────────────────────────────────────────────
run_docker() {
    ensure_docker_images 0

    if ! docker_image_present "$TITO_IMAGE"; then
        echo "ERROR: Docker image missing: $TITO_IMAGE" >&2
        echo "  ./tito-run.sh load-images" >&2
        exit 1
    fi
    if ! docker_image_present "$EF5_DOCKER_IMAGE"; then
        echo "ERROR: Docker image missing: $EF5_DOCKER_IMAGE" >&2
        echo "  ./tito-run.sh load-images" >&2
        exit 1
    fi

    local args=()
    for d in "${DATA_MOUNTS[@]}"; do
        mkdir -p "$SCRIPT_DIR/$d"
        args+=(-v "$HOST_PROJECT/$d:/app/$d")
    done
    for d in basic parameters pet templates states precip precipEF5 qpf_store; do
        mkdir -p "$SCRIPT_DIR/EF5_conf/$d"
    done

    # docker.sock — Docker Desktop (Mac/Win) and Linux Engine
    local sock="${DOCKER_HOST_SOCK:-/var/run/docker.sock}"
    if [[ ! -S "$sock" ]] && [[ "$HOST_OS" == "windows" ]]; then
        # Git Bash sometimes sees the socket via this path under Docker Desktop
        sock="/var/run/docker.sock"
    fi

    args+=(
        -v "$HOST_PROJECT/Caribbean_Comoros_config.py:/app/Caribbean_Comoros_config.py:ro"
        -v "$HOST_PROJECT/orchestrator.py:/app/orchestrator.py:ro"
        -v "$HOST_PROJECT/hindcast_manager.py:/app/hindcast_manager.py:ro"
        -v "$HOST_PROJECT/tito_utils:/app/tito_utils:rw"
        -v "$HOST_PROJECT/EF5:/app/EF5:ro"
        -v "${sock}:/var/run/docker.sock"
        -e EF5_RUNTIME=docker
        -e "EF5_IMAGE=$EF5_DOCKER_IMAGE"
        -e "TITO_HOST_PROJECT=$HOST_PROJECT"
        -e TITO_FIM_ROOT=/app
        -e PYTHONUNBUFFERED=1
        -e TZ=Etc/UTC
        -e STORMLAB_USE_TITO_ENV=1
        --rm
    )
    # Forward offline training mode into the container
    if [[ "${TITO_OFFLINE:-}" == "1" ]] || printf '%s\n' "$@" | grep -qx -- '--offline'; then
        args+=(-e TITO_OFFLINE=1)
        args+=(-e "TITO_OFFLINE_PRECIP=${TITO_OFFLINE_PRECIP:-/app/offline_precips}")
        # /app for `import offline`; /app/offline so sitecustomize.py auto-loads
        args+=(-e "PYTHONPATH=/app:/app/offline${PYTHONPATH:+:$PYTHONPATH}")
    fi

    # --network host is reliable only on native Linux.
    # Docker Desktop (Mac/Windows) uses a VM; default bridge still has outbound net.
    if [[ "$HOST_OS" == "linux" ]]; then
        args+=(--network host)
    fi

    echo "==== TITO launcher ===="
    echo "  Runtime : docker ($HOST_OS)"
    echo "  Project : $HOST_PROJECT"
    echo "  Image   : $TITO_IMAGE"
    echo "  EF5     : $EF5_DOCKER_IMAGE (sibling via docker.sock)"
    if [[ "$HOST_OS" != "linux" ]]; then
        echo "  Network : bridge (Docker Desktop)"
    else
        echo "  Network : host"
    fi

    exec docker run "${args[@]}" "$TITO_IMAGE" "$@"
}

# ── Apptainer / Singularity — Linux HPC only ───────────────────────────────
run_apptainer() {
    local cmd="$1"; shift
    if [[ ! -f "$TITO_SIF" ]]; then
        echo "ERROR: TITO SIF not found: $TITO_SIF" >&2
        echo "  ./docker-to-apptainer.sh" >&2
        exit 1
    fi
    if [[ ! -x "$EF5_LOCAL_BIN" ]]; then
        echo "ERROR: glibc EF5 binary missing: $EF5_LOCAL_BIN" >&2
        echo "  Build once on a Docker host: ./EF5/docker/build_ef5_local.sh" >&2
        exit 1
    fi

    local binds=()
    for d in "${DATA_MOUNTS[@]}"; do
        mkdir -p "$SCRIPT_DIR/$d"
        binds+=(--bind "$SCRIPT_DIR/$d:/app/$d")
    done
    for d in basic parameters pet templates states precip precipEF5 qpf_store; do
        mkdir -p "$SCRIPT_DIR/EF5_conf/$d"
    done
    binds+=(
        --bind "$SCRIPT_DIR/Caribbean_Comoros_config.py:/app/Caribbean_Comoros_config.py:ro"
        --bind "$SCRIPT_DIR/orchestrator.py:/app/orchestrator.py:ro"
        --bind "$SCRIPT_DIR/hindcast_manager.py:/app/hindcast_manager.py:ro"
        --bind "$SCRIPT_DIR/tito_utils:/app/tito_utils"
        --bind "$SCRIPT_DIR/EF5:/app/EF5:ro"
        --bind "$SCRIPT_DIR/docker-entrypoint.sh:/app/docker-entrypoint.sh:ro"
    )

    # --cleanenv drops host env; pass offline flags explicitly when requested
    local env_csv="EF5_RUNTIME=local,EF5_LOCAL_BIN=/app/EF5/bin/ef5,TITO_FIM_ROOT=/app,PYTHONUNBUFFERED=1,TZ=Etc/UTC,STORMLAB_USE_TITO_ENV=1"
    local offline=0
    if [[ "${TITO_OFFLINE:-}" == "1" ]] || printf '%s\n' "$@" | grep -qx -- '--offline'; then
        offline=1
        env_csv+=",TITO_OFFLINE=1,TITO_OFFLINE_PRECIP=/app/offline_precips,TITO_OFFLINE_CONFIG=Caribbean_Comoros_config,PYTHONPATH=/app:/app/offline"
    fi

    echo "==== TITO launcher ===="
    echo "  Runtime : $cmd"
    echo "  Project : $SCRIPT_DIR"
    echo "  SIF     : $TITO_SIF"
    echo "  EF5     : $EF5_LOCAL_BIN (in-process, no nested Apptainer)"
    if [[ "$offline" == "1" ]]; then
        echo "  Offline : YES (no precip downloads)"
    fi

    exec "$cmd" run --cleanenv \
        --env "$env_csv" \
        "${binds[@]}" \
        --pwd /app \
        "$TITO_SIF" "$@"
}

# ── Native conda (dev only) ────────────────────────────────────────────────
run_native() {
    echo "==== TITO launcher ===="
    echo "  Runtime : native conda"
    echo "  Project : $SCRIPT_DIR"
    # shellcheck disable=SC1091
    if [[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]]; then
        source "$HOME/miniconda3/etc/profile.d/conda.sh"
    elif [[ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]]; then
        source "$HOME/anaconda3/etc/profile.d/conda.sh"
    elif [[ -f "/opt/conda/etc/profile.d/conda.sh" ]]; then
        source "/opt/conda/etc/profile.d/conda.sh"
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

# ── Entry ──────────────────────────────────────────────────────────────────
MODE_OR_CMD="${1:-operational}"

case "$MODE_OR_CMD" in
    load-images|load_images|load)
        shift || true
        cmd_load_images
        exit 0
        ;;
    help|-h|--help)
        sed -n '2,40p' "$0"
        exit 0
        ;;
esac

RUNTIME="$(detect_runtime)"

case "$RUNTIME" in
    docker)                run_docker "$@" ;;
    apptainer|singularity) run_apptainer "$RUNTIME" "$@" ;;
    native)                run_native "$@" ;;
    *) echo "Unknown runtime: $RUNTIME"; exit 1 ;;
esac
