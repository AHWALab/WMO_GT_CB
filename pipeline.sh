#!/usr/bin/bash
# Hourly TITO Caribbean & Comoros operational runner.
# Intended to be called by cron once per hour, e.g.:
#   0 * * * * /path/to/TITO_Caribbean_Comoros/pipeline.sh
#
# The script:
#   1. Waits up to WAIT_MINUTES (default 5) for data to arrive
#   2. Activates the conda environment
#   3. Runs orchestrator.py with the correct config
#   4. Logs all output to outputs/logs/tito_<timestamp>.log
#
# Non-fatal errors (orchestrator exits non-zero) are logged but do NOT abort
# the cron job — use set -uo pipefail without -e so the log always captures them.
set -uo pipefail
shopt -s nullglob

WAIT_MINUTES=0   # cron offset (hh:07 via manage_cron.sh) is the primary data-arrival gate
CONFIG="Caribbean_Comoros_config.py"
CONDA_ENV="tito_env2"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$PROJECT_ROOT/outputs/logs"
mkdir -p "$LOG_DIR"
RUN_TS=$(date -u +%Y%m%dT%H%M%S)
LOG_FILE="$LOG_DIR/tito_hourly_${RUN_TS}.log"

exec > >(stdbuf -oL -eL tee -a "$LOG_FILE") 2>&1

echo "==== TITO hourly run started at $(date -u --iso-8601=seconds) ===="
echo "Config : $CONFIG"
echo "Log    : $LOG_FILE"

# ── Wait for data arrival ────────────────────────────────────────────────────
echo "Waiting ${WAIT_MINUTES} min for data arrival..."
sleep $(( WAIT_MINUTES * 60 ))

# ── Locate and activate Conda (cron-safe) ───────────────────────────────────
CONDA_BASE=""
for cand in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" "/opt/conda"; do
  if [ -d "$cand" ]; then
    CONDA_BASE="$cand"
    break
  fi
done

if [ -n "$CONDA_BASE" ] && [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
  # shellcheck disable=SC1090
  source "$CONDA_BASE/etc/profile.d/conda.sh"
elif [ -n "$CONDA_BASE" ] && [ -x "$CONDA_BASE/bin/conda" ]; then
  export PATH="$CONDA_BASE/bin:$PATH"
  eval "$("$CONDA_BASE/bin/conda" shell.bash hook 2>/dev/null)" || true
fi

# Fallback: source user bashrc if conda still not available
if ! command -v conda >/dev/null 2>&1; then
  if [ -f "$HOME/.bashrc" ]; then
    # shellcheck disable=SC1090
    source "$HOME/.bashrc"
  fi
  if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook 2>/dev/null)" || true
  fi
fi

if ! command -v conda >/dev/null 2>&1; then
  echo "ERROR: conda not found — install Conda or adjust PATH in this script."
  exit 1
fi

echo "Activating conda env '$CONDA_ENV'..."
set +u
conda activate "$CONDA_ENV"
set -u

# ── Run TITO ────────────────────────────────────────────────────────────────
echo "Running orchestrator with config: $CONFIG"
cd "$PROJECT_ROOT"
PYTHONUNBUFFERED=1 python orchestrator.py "$CONFIG" && \
  echo "Orchestrator completed successfully." || \
  echo "WARNING: orchestrator exited with a non-zero status — check log above."

set +u
conda deactivate || true
set -u

echo "==== TITO hourly run finished at $(date -u --iso-8601=seconds) ===="