#!/usr/bin/env bash
# Load pre-built TITO + EF5 images from dist/
# Wrapper around: ./tito-run.sh load-images
#
# Always invokes tito-run.sh via bash so missing +x (USB/zip/FAT) does not
# cause "Permission denied" on macOS.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(uname -s 2>/dev/null || true)" == Darwin ]]; then
    xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true
fi
chmod +x "$ROOT/tito-run.sh" "$ROOT/load-docker-images.sh" 2>/dev/null || true
exec bash "$ROOT/tito-run.sh" load-images "$@"
