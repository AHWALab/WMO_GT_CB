#!/bin/bash
#$ -o tito_orchestrator_output.txt
#$ -j y
#$ -cwd
#$ -pe smp 56
#$ -M naman-mehta@uiowa.edu
#$ -m be
#$ -N tito_orchestrator

# ── Activate conda environment ──────────────────────────────────────────
source /Users/nammehta/miniconda3/etc/profile.d/conda.sh
conda activate tito_env2

# ── TITO orchestrator ───────────────────────────────────────────────────
# Usage: python orchestrator.py <config_file>
# The config file is imported as a Python module (omit .py extension).
python /Dedicated/Humberto/WMO_Caribbean_Comoros/TITO/TITO_Stream_Sat/orchestrator.py
