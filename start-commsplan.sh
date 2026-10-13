#!/usr/bin/env bash
# Preserve the legacy launcher's installation, profile, receipt, and exit behavior.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/start-freqinout.sh" "$@"
