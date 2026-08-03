#!/usr/bin/env bash
# ============================================================================
# Build glibc EF5 binary → EF5/bin/ef5  (runs inside TITO Docker/Apptainer)
# ============================================================================
# Why: Apptainer cannot reliably nest Apptainer→EF5 SIF on HPC (setuid /
# session dirs). Docker partners still use docker.sock + ef5-container;
# Apptainer partners use this local binary with EF5_RUNTIME=local.
#
# Usage (on a machine with Docker):
#   ./EF5/docker/build_ef5_local.sh
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EF5_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$EF5_DIR/bin"
OUT_BIN="$OUT_DIR/ef5"
IMAGE="${EF5_UBUNTU_IMAGE:-ef5-ubuntu:local}"

mkdir -p "$OUT_DIR"

echo ">>> Building $IMAGE from Dockerfile.ubuntu"
docker build -f "$SCRIPT_DIR/Dockerfile.ubuntu" -t "$IMAGE" "$SCRIPT_DIR"

echo ">>> Extracting /ef5/bin/ef5 → $OUT_BIN"
CID="$(docker create "$IMAGE")"
docker cp "$CID:/ef5/bin/ef5" "$OUT_BIN"
docker rm "$CID" >/dev/null
chmod +x "$OUT_BIN"

echo ">>> Smoke-test linkage (host may miss libs; TITO image has them)"
file "$OUT_BIN"
ls -lh "$OUT_BIN"
echo "Done. Apptainer path: EF5_RUNTIME=local uses $OUT_BIN"
