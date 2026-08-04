#!/bin/bash
set -euo pipefail

ROOT="/Dedicated/Humberto/WMO_Caribbean_Comoros/TITO/TITO_GuatemalaTraining"

TARGETS=(
  "$ROOT/EF5_conf/precip"
  "$ROOT/EF5_conf/qpf_store"
  "$ROOT/EF5_conf/states"
  "$ROOT/outputs"
  "$ROOT/tito_utils/qpe_utils/STREAM-Sat-realtime/extension/realtime/output/caribbean"
  "$ROOT/tito_utils/qpe_utils/STREAM-Sat-realtime/extension/realtime/state"
  "$ROOT/tito_utils/qpf_utils/StormLab-GFS-realtime/output/guatemala"
)

DRY_RUN="${DRY_RUN:-1}"

clean_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    echo "SKIP (missing): $dir"
    return
  fi

  local removed=0
  local kept=0
  while IFS= read -r -d '' entry; do
    local name
    name="$(basename "$entry")"
    if [ "$name" = ".gitkeep" ]; then
      kept=$((kept + 1))
      continue
    fi
    removed=$((removed + 1))
    if [ "$DRY_RUN" = "1" ]; then
      echo "WOULD REMOVE: $entry"
    else
      rm -rf -- "$entry"
    fi
  done < <(find "$dir" -mindepth 1 -maxdepth 1 -print0)

  echo "$dir -> removed $removed, kept $kept"
}

for t in "${TARGETS[@]}"; do
  clean_dir "$t"
done

echo ""
if [ "$DRY_RUN" = "1" ]; then
  echo "Dry run complete. Re-run with DRY_RUN=0 to actually delete."
else
  echo "Cleanup complete."
fi
