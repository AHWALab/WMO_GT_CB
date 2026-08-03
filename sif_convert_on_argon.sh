#!/usr/bin/env bash
# Run this ON ARGON (has Apptainer, no Docker) to convert archives → SIF
set -euo pipefail
cd "$(dirname "$0")/.."
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
CMD=$(command -v apptainer || command -v singularity)
echo "Using: $CMD"
gunzip -c dist/docker-archives/tito_latest.tar.gz > /tmp/tito_latest.tar
$CMD build --force tito.sif docker-archive:///tmp/tito_latest.tar
rm -f /tmp/tito_latest.tar
gunzip -c dist/docker-archives/ef5-container_latest.tar.gz > /tmp/ef5.tar
mkdir -p EF5
$CMD build --force EF5/ef5-container.sif docker-archive:///tmp/ef5.tar
rm -f /tmp/ef5.tar
ls -lh tito.sif EF5/ef5-container.sif
echo "Done. Test: ./tito-run.sh operational --regions Guatemala"
