#!/usr/bin/env bash
# ============================================================================
# reset_tito.sh — wipe training run products (Linux / macOS / Git Bash)
# ============================================================================
#   ./reset_tito.sh              # delete
#   ./reset_tito.sh --dry-run    # print only
#   ./reset_tito.sh --force      # if no keep-timestamp tifs found, still delete other tifs
#
# Wipes contents of outputs, precip, precipEF5, qpf_store, STREAM-Sat and
# StormLab output folders. Keeps the folders themselves and .gitkeep.
#
# States: never delete folders. Only delete *.tif that are NOT the training
# warmup snapshot (20230619 15:00). Matches these name spellings:
#   20230619_1500  20230619_150000  20230619.1500  20230619.150000  202306191500
# If none match, state tifs are left alone (unless --force).
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --force|-f)   FORCE=1 ;;
    esac
done
if [[ "${DRY_RUN:-0}" == "1" ]]; then DRY_RUN=1; fi

WIPE_DIRS=(
    "$SCRIPT_DIR/outputs"
    "$SCRIPT_DIR/EF5_conf/precip"
    "$SCRIPT_DIR/EF5_conf/precipEF5"
    "$SCRIPT_DIR/EF5_conf/qpf_store"
    "$SCRIPT_DIR/tito_utils/qpe_utils/STREAM-Sat-realtime/extension/realtime/output/caribbean"
    "$SCRIPT_DIR/tito_utils/qpf_utils/StormLab-GFS-realtime/output/guatemala"
)
STATES_DIR="$SCRIPT_DIR/EF5_conf/states"

is_keep_tif() {
    local b="$1"
    case "$b" in
        *20230619_1500*|*20230619_150000*|*20230619.1500*|*20230619.150000*|*202306191500*) return 0 ;;
        *) return 1 ;;
    esac
}

echo "==== reset_tito ===="
echo "  Root   : $SCRIPT_DIR"
echo "  Mode   : $([ "$DRY_RUN" -eq 1 ] && echo DRY-RUN || echo DELETE)"
echo "  Keep   : states *.tif for 2023-06-19 15:00 (any common spelling)"
echo ""

wipe_dir() {
    local dir="$1"
    if [[ ! -d "$dir" ]]; then
        echo "SKIP (missing): $dir"
        return
    fi
    local n=0
    local entry
    while IFS= read -r -d '' entry; do
        local name
        name="$(basename "$entry")"
        if [[ "$name" == ".gitkeep" ]]; then
            continue
        fi
        n=$((n + 1))
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo "  WOULD REMOVE: $entry"
        else
            rm -rf -- "$entry"
        fi
    done < <(find "$dir" -mindepth 1 -maxdepth 1 -print0)
    echo "WIPE  $dir  ($n item(s))"
}

for d in "${WIPE_DIRS[@]}"; do
    wipe_dir "$d"
done

echo ""
if [[ ! -d "$STATES_DIR" ]]; then
    echo "SKIP (missing): $STATES_DIR"
else
    local_keep=0
    local_del=0
    sample_n=0
    while IFS= read -r -d '' tif; do
        base="$(basename "$tif")"
        if is_keep_tif "$base"; then
            local_keep=$((local_keep + 1))
            echo "    KEEP  $tif"
        else
            local_del=$((local_del + 1))
        fi
    done < <(find "$STATES_DIR" -type f \( -iname '*.tif' -o -iname '*.tiff' \) -print0)

    echo "STATES scan: $((local_keep + local_del)) tif(s) — keep $local_keep  delete $local_del"

    if [[ "$local_del" -gt 0 && "$local_keep" -eq 0 && "$FORCE" -ne 1 ]]; then
        echo "ERROR: no state tif matched 2023-06-19 15:00 — refusing to delete any state tifs."
        echo "  Sample names in $STATES_DIR:"
        while IFS= read -r -d '' tif; do
            echo "    $(basename "$tif")"
            sample_n=$((sample_n + 1))
            [[ "$sample_n" -ge 8 ]] && break
        done < <(find "$STATES_DIR" -type f \( -iname '*.tif' -o -iname '*.tiff' \) -print0)
        echo "  Restore/check the warmup files, then re-run. Or pass --force to delete all non-matching tifs anyway."
    else
        while IFS= read -r -d '' tif; do
            base="$(basename "$tif")"
            if is_keep_tif "$base"; then
                continue
            fi
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo "  WOULD DELETE TIF: $tif"
            else
                rm -f -- "$tif"
            fi
        done < <(find "$STATES_DIR" -type f \( -iname '*.tif' -o -iname '*.tiff' \) -print0)
        echo "STATES $STATES_DIR  deleted $local_del tif(s), kept $local_keep, folders untouched"
    fi
fi

echo ""
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "Dry run only. Re-run without --dry-run to delete."
else
    echo "Reset complete."
fi
