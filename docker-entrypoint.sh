#!/usr/bin/env bash
# ============================================================================
# TITO Docker Entrypoint
# ============================================================================
# Usage:
#   docker run ... tito:latest                                    # operational, all regions
#   docker run ... tito:latest operational                        # explicit operational
#   docker run ... tito:latest operational --regions Guatemala    # single region
#   docker run ... tito:latest hindcast "START" "END"                         # all regions
#   docker run ... tito:latest hindcast "START" "END" --regions Guatemala     # single region
#   docker run ... tito:latest shell                              # interactive bash
# ============================================================================
set -euo pipefail

# ── Activate conda environment ─────────────────────────────────────────────
source /opt/conda/etc/profile.d/conda.sh
conda activate tito_env2

cd /app

# ── Parse common optional flags ────────────────────────────────────────────
REGIONS_ARG=""
OFFLINE=0
REMAINING_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --regions)
            REGIONS_ARG="--regions $2"
            shift 2
            ;;
        --offline)
            OFFLINE=1
            shift
            ;;
        *)
            REMAINING_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${REMAINING_ARGS[@]}"

MODE="${1:-operational}"
shift || true

if [[ "$OFFLINE" == "1" ]]; then
    export TITO_OFFLINE=1
    export TITO_OFFLINE_PRECIP="${TITO_OFFLINE_PRECIP:-/app/offline_precips}"
    export TITO_OFFLINE_CONFIG=Caribbean_Comoros_config
    export PYTHONPATH="/app:/app/offline${PYTHONPATH:+:$PYTHONPATH}"
    echo "==== OFFLINE training mode (no precip downloads) ===="
fi

case "$MODE" in
    operational)
        echo "==== TITO Operational Mode ===="
        echo "Cycle: $(date -u --iso-8601=seconds)"
        [ -n "$REGIONS_ARG" ] && echo "Regions: ${REGIONS_ARG#--regions }"
        if [[ "$OFFLINE" == "1" ]]; then
            echo "NOTE: --offline is intended for the fixed training hindcast window."
        fi
        exec python orchestrator.py Caribbean_Comoros_config.py $REGIONS_ARG
        ;;

    hindcast)
        HINDCAST_START="${1:?Usage: hindcast 'YYYY-MM-DD HH:MM' 'YYYY-MM-DD HH:MM'}"
        HINDCAST_END="${2:?Usage: hindcast 'YYYY-MM-DD HH:MM' 'YYYY-MM-DD HH:MM'}"
        echo "==== TITO Hindcast Mode ===="
        echo "From: $HINDCAST_START"
        echo "To:   $HINDCAST_END"
        [ -n "$REGIONS_ARG" ] && echo "Regions: ${REGIONS_ARG#--regions }"
        if [[ "$OFFLINE" == "1" ]]; then
            exec python offline/run_offline_hindcast.py \
                "$HINDCAST_START" "$HINDCAST_END" $REGIONS_ARG
        fi
        exec python hindcast_manager.py Caribbean_Comoros_config.py "$HINDCAST_START" "$HINDCAST_END" $REGIONS_ARG
        ;;

    shell|bash)
        echo "==== TITO Interactive Shell ===="
        echo "Conda env: tito_env2"
        echo "EF5 runtime: $(python -c "from tito_utils.ef5.ef5_routines import _detect_container_runtime; print(_detect_container_runtime())" 2>/dev/null || echo 'unknown')"
        exec /bin/bash
        ;;

    *)
        echo "Unknown mode: $MODE"
        echo "Usage: docker run ... tito:latest [operational|hindcast START END|shell] [--regions R1,R2]"
        exit 1
        ;;
esac
