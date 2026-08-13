#!/usr/bin/env bash
# Freeze a copy of live training precip into offline_precips/
# (run once after a successful online hindcast that produced precip).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OFF="$ROOT/offline_precips"
mkdir -p "$OFF"
echo "Materializing offline precip archive → $OFF"
rsync -a --delete "$ROOT/EF5_conf/precip/stream_sat/" "$OFF/stream_sat/"
rsync -a --delete "$ROOT/EF5_conf/precip/stormlab/"  "$OFF/stormlab/"
rsync -a --delete "$ROOT/EF5_conf/precip/imerg/"     "$OFF/imerg/"
rsync -a --delete "$ROOT/EF5_conf/qpf_store/"        "$OFF/qpf_store/"
du -sh "$OFF"/* 2>/dev/null || true
echo "Done. Use: ./tito-run.sh hindcast \"2023-06-21 07:00\" \"2023-06-21 08:00\" --regions Guatemala --offline"
