#!/usr/bin/env bash
set -Eeuo pipefail

# Backward-compatible entry point for existing systemd units. New deployments
# should call run_eduprog.sh directly.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec /bin/bash "$SCRIPT_DIR/run_eduprog.sh" "$@"
