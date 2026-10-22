#!/usr/bin/env bash
# Supported Linux maintenance entry; legacy implementation remains compatible.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
action="setup"
if [[ $# -gt 0 && "$1" != -* ]]; then action="$1"; shift; fi
case "$action" in
  profile)
    case "${1:---help}" in
      plan|copy|journals|recover) profile_command="profile-$1"; shift ;;
      --help|-h) echo "Usage: install-commsplan.sh profile plan|copy|journals|recover --help"; exit 0 ;;
      *) echo "Use profile plan, profile copy, profile journals or profile recover." >&2; exit 2 ;;
    esac
    python="$SCRIPT_DIR/.venv/bin/python"
    if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then python="$SCRIPT_DIR/venv/bin/python"; fi
    [[ -x "$python" ]] || { echo "Run setup before using profile migration." >&2; exit 1; }
    exec "$python" "$SCRIPT_DIR/start-commsplan-migration.py" "$profile_command" "$@" ;;
  setup) mode=--local ;;
  update) mode=--update-current ;;
  repair) mode=--repair ;;
  desktop) mode=--desktop-only ;;
  uninstall) exec bash "$SCRIPT_DIR/uninstall_FreqInOut_linux.sh" --dir "$SCRIPT_DIR" "$@" ;;
  *) echo "Unknown action: $action. Use setup, update, repair, desktop, uninstall or profile." >&2; exit 2 ;;
esac
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'EOF'
CommsPlan Linux installation and maintenance
Usage: bash install-commsplan.sh [setup|update|repair|desktop|uninstall|profile] [options]
  setup     Verify/install this checkout and refresh launcher/icon (default).
  update    Fast-forward its current branch/upstream, verify, refresh launcher/icon.
  repair    Repair dependencies/receipt and refresh launcher/icon; no Git update.
  desktop   Refresh launcher/icon from local assets; no Git or dependency update.
  profile   Preview/copy profiles: profile plan or profile copy --help.
  uninstall Use the guarded uninstaller; profile data is retained.
Default directory: the folder containing this script, including existing installation folders.
Options: --dir PATH, --config-root PATH, --relocate-from OLD_PATH (setup only), --dry-run, --yes; legacy installer options supported.
An explicit --repo/--branch selects another update source. Otherwise update requires an upstream.
The existing profile location is preserved, including explicit or remembered selections.
No profile directory or database files are renamed. Close the application before setup/update/repair.
EOF
  exit 0
fi
exec bash "$SCRIPT_DIR/install_FreqInOut_linux.sh" --dir "$SCRIPT_DIR" "$mode" "$@"
