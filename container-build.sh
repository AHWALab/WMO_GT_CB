#!/usr/bin/env bash
# ============================================================================
# TITO container build — Docker + Apptainer for partner distribution
# ============================================================================
# You build ONCE on a machine that has Docker (and ideally Apptainer).
# Partners then run with whatever they have:
#   - Docker      → load tar / use image + ./tito-run.sh
#   - Apptainer   → use .sif files + ./tito-run.sh
#
# Usage:
#   ./container-build.sh                      # Docker EF5 + Docker TITO
#   ./container-build.sh --partner            # FULL partner bundle (Docker+SIF+tars)
#   ./container-build.sh --sif                # also convert images → .sif
#   ./container-build.sh --no-ef5             # skip EF5 docker rebuild
#   ./container-build.sh --no-cache
# ============================================================================
set -euo pipefail
# Fail the script if docker build fails even when piped through tee
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_EF5=true
BUILD_DOCKER=true
BUILD_SIF=false
PARTNER_BUNDLE=false
NO_CACHE=""

for arg in "$@"; do
    case "$arg" in
        --no-ef5) BUILD_EF5=false ;;
        --sif) BUILD_SIF=true ;;
        --sif-only) BUILD_DOCKER=false; BUILD_EF5=false; BUILD_SIF=true ;;
        --partner) PARTNER_BUNDLE=true; BUILD_SIF=true; BUILD_DOCKER=true; BUILD_EF5=true ;;
        --no-cache) NO_CACHE="--no-cache" ;;
        -h|--help)
            sed -n '2,22p' "$0"
            exit 0
            ;;
    esac
done

echo "=============================================="
echo "  TITO Container Build"
echo "=============================================="
echo "  Project root   : $SCRIPT_DIR"
echo "  Build EF5      : $BUILD_EF5"
echo "  Build Docker   : $BUILD_DOCKER"
echo "  Build SIF      : $BUILD_SIF"
echo "  Partner bundle : $PARTNER_BUNDLE"
echo "  No cache       : ${NO_CACHE:-false}"
echo "=============================================="

have_docker() { command -v docker >/dev/null 2>&1; }

apptainer_cmd() {
    if command -v apptainer >/dev/null 2>&1; then
        echo apptainer
    elif command -v singularity >/dev/null 2>&1; then
        echo singularity
    else
        echo ""
    fi
}

# ── Step 1: EF5 Docker ─────────────────────────────────────────────────────
if $BUILD_EF5; then
    if ! have_docker; then
        echo "ERROR: docker not found. Partner images must be built on a Docker host."
        echo "Then copy the bundle (SIFs / docker tars) to HPC or partner machines."
        exit 1
    fi
    echo ""
    echo ">>> STEP 1: Building EF5 (ef5-container:latest) ..."
    cd "$SCRIPT_DIR/EF5/docker"
    docker build $NO_CACHE -t ef5-container:latest .
    cd "$SCRIPT_DIR"
    echo ">>> EF5 Docker image ready."
    echo ">>> Also building glibc EF5 binary for Apptainer partners (no nesting) ..."
    bash "$SCRIPT_DIR/EF5/docker/build_ef5_local.sh"
else
    echo ""
    echo ">>> STEP 1: Skipping EF5 Docker build."
    if [[ ! -x "$SCRIPT_DIR/EF5/bin/ef5" ]] && have_docker; then
        echo ">>> Building glibc EF5 binary (EF5/bin/ef5) for Apptainer ..."
        bash "$SCRIPT_DIR/EF5/docker/build_ef5_local.sh"
    fi
fi

# ── Step 2: TITO Docker ────────────────────────────────────────────────────
if $BUILD_DOCKER; then
    if ! have_docker; then
        echo "ERROR: docker not found — cannot build tito:latest."
        exit 1
    fi
    echo ""
    echo ">>> STEP 2: Building TITO (tito:latest) ..."
    echo "    Expect 10–20 min (conda + PyTorch)."
    docker build $NO_CACHE -t tito:latest .
    echo ">>> TITO Docker image ready."
else
    echo ""
    echo ">>> STEP 2: Skipping TITO Docker build."
fi

# ── Step 3: Docker → Apptainer SIF (partners without Docker) ───────────────
if $BUILD_SIF; then
    echo ""
    echo ">>> STEP 3: Converting Docker images → Apptainer SIF ..."
    if ! have_docker; then
        echo "ERROR: docker required for docker-daemon:// conversion."
        exit 1
    fi
    APPTAINER_CMD="$(apptainer_cmd)"
    if [[ -z "$APPTAINER_CMD" ]]; then
        echo "ERROR: apptainer/singularity not found."
        echo "Install Apptainer on the build host, or convert SIFs elsewhere."
        exit 1
    fi

    if docker image inspect tito:latest >/dev/null 2>&1; then
        echo "    → tito.sif"
        $APPTAINER_CMD build --force "$SCRIPT_DIR/tito.sif" docker-daemon://tito:latest
    else
        echo "ERROR: tito:latest missing."
        exit 1
    fi

    if docker image inspect ef5-container:latest >/dev/null 2>&1; then
        echo "    → EF5/ef5-container.sif"
        mkdir -p "$SCRIPT_DIR/EF5"
        $APPTAINER_CMD build --force "$SCRIPT_DIR/EF5/ef5-container.sif" \
            docker-daemon://ef5-container:latest
    else
        echo "WARNING: ef5-container:latest missing — skipping EF5 SIF."
    fi
    echo ">>> SIF conversion done."
else
    echo ""
    echo ">>> STEP 3: Skipping SIF (use --sif or --partner)."
fi

# ── Step 4: Partner distribution bundle ────────────────────────────────────
if $PARTNER_BUNDLE; then
    echo ""
    echo ">>> STEP 4: Assembling partner bundle under dist/tito-partner/ ..."
    DIST="$SCRIPT_DIR/dist/tito-partner"
    rm -rf "$DIST"
    mkdir -p "$DIST/EF5" "$DIST/docker-images"

    # Launcher + compose + entry config
    cp -a "$SCRIPT_DIR/tito-run.sh" "$DIST/"
    cp -a "$SCRIPT_DIR/docker-compose.yml" "$DIST/"
    cp -a "$SCRIPT_DIR/docker-entrypoint.sh" "$DIST/"
    cp -a "$SCRIPT_DIR/Caribbean_Comoros_config.py" "$DIST/"
    cp -a "$SCRIPT_DIR/orchestrator.py" "$DIST/"
    cp -a "$SCRIPT_DIR/hindcast_manager.py" "$DIST/"
    cp -a "$SCRIPT_DIR/tito_utils" "$DIST/"
    cp -a "$SCRIPT_DIR/templates" "$DIST/" 2>/dev/null || true

    # Apptainer images (HPC / no-Docker partners)
    [[ -f "$SCRIPT_DIR/tito.sif" ]] && cp -a "$SCRIPT_DIR/tito.sif" "$DIST/"
    [[ -f "$SCRIPT_DIR/EF5/ef5-container.sif" ]] && \
        cp -a "$SCRIPT_DIR/EF5/ef5-container.sif" "$DIST/EF5/"
    # glibc EF5 binary (Apptainer path — runs inside TITO SIF, no nesting)
    if [[ -x "$SCRIPT_DIR/EF5/bin/ef5" ]]; then
        mkdir -p "$DIST/EF5/bin"
        cp -a "$SCRIPT_DIR/EF5/bin/ef5" "$DIST/EF5/bin/"
    fi

    # Docker image tarballs (partners with Docker, no registry)
    if have_docker; then
        echo "    Saving Docker image tarballs (large) ..."
        docker save tito:latest | gzip > "$DIST/docker-images/tito_latest.tar.gz"
        docker save ef5-container:latest | gzip > "$DIST/docker-images/ef5-container_latest.tar.gz"
    fi

    # Empty data dirs partners must populate / mount
    mkdir -p "$DIST/outputs"
    touch "$DIST/outputs/.gitkeep"
    for d in basic parameters pet templates states precip precipEF5 qpf_store; do
        mkdir -p "$DIST/EF5_conf/$d"
        touch "$DIST/EF5_conf/$d/.gitkeep"
    done

    cat > "$DIST/README_PARTNER.txt" <<'EOF'
TITO partner container package
==============================

This bundle runs the FULL TITO system in containers.
Partners need EITHER Docker OR Apptainer/Singularity — not both.

------------------------------------------------
A) Partner HAS Docker
------------------------------------------------
  1. Load images (once):
       gunzip -c docker-images/tito_latest.tar.gz | docker load
       gunzip -c docker-images/ef5-container_latest.tar.gz | docker load

  2. Put your DEM/params/PET into:
       EF5_conf/basic/  EF5_conf/parameters/  EF5_conf/pet/  EF5_conf/templates/

  3. Run:
       ./tito-run.sh operational --regions Guatemala
       # or:
       docker-compose run --rm tito operational --regions Guatemala

------------------------------------------------
B) Partner has NO Docker (HPC / Apptainer only)  ← like Argon
------------------------------------------------
  1. Confirm files exist:
       tito.sif
       EF5/bin/ef5          # glibc binary (NO nested Apptainer)

  2. Put DEM/params/PET into:
       EF5_conf/basic/  EF5_conf/parameters/  EF5_conf/pet/  EF5_conf/templates/

  3. Run:
       TITO_RUNTIME=apptainer ./tito-run.sh operational --regions Guatemala

  EF5 runs as EF5_RUNTIME=local inside the TITO SIF (same process tree).
  Nested Apptainer→Apptainer is NOT used (HPC setuid/session failures).

------------------------------------------------
C) What gets mounted (writable outputs)
------------------------------------------------
  EF5_conf/  (basic, parameters, pet, templates, states, precip, precipEF5, qpf_store)
  outputs/

Edit Caribbean_Comoros_config.py for regions, ensemble size, credentials.

------------------------------------------------
Why we build SIFs on a Docker host (not on HPC)
------------------------------------------------
HPC nodes often block Docker. We BUILD the images where Docker exists,
CONVERT them to .sif, then COPY the .sif files to HPC. Partners never
need to build — they only RUN.
EOF

    chmod +x "$DIST/tito-run.sh" "$DIST/docker-entrypoint.sh" 2>/dev/null || true

    echo ">>> Partner bundle ready: $DIST"
    du -sh "$DIST" "$DIST"/tito.sif "$DIST"/EF5/*.sif "$DIST"/docker-images/* 2>/dev/null || true
fi

echo ""
echo "=============================================="
echo "  Build complete"
echo "=============================================="
if have_docker; then
    docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" \
        | grep -E "ef5-container|tito|REPOSITORY" || true
fi
[[ -f "$SCRIPT_DIR/tito.sif" ]] && ls -lh "$SCRIPT_DIR/tito.sif"
[[ -f "$SCRIPT_DIR/EF5/ef5-container.sif" ]] && ls -lh "$SCRIPT_DIR/EF5/ef5-container.sif"

echo ""
echo "  Partner build (recommended on Docker host):"
echo "    ./container-build.sh --partner"
echo ""
echo "  Run anywhere:"
echo "      TITO_RUNTIME=docker     ./tito-run.sh operational --regions Guatemala"
echo "      TITO_RUNTIME=apptainer  ./tito-run.sh operational --regions Guatemala"


