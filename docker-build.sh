#!/usr/bin/env bash
# Back-compat wrapper — prefer ./container-build.sh
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/container-build.sh" "$@"
