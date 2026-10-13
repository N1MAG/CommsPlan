#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/FreqInOut}"
LOG_FILE="${LOG_FILE:-$HOME/freqinout-uninstall.log}"
ASSUME_YES=0
DRY_RUN=0

LAUNCHER_PATH="$HOME/.local/bin/freqinout"
DESKTOP_FILE_NAME="freqinout.desktop"
DESKTOP_ENTRY_PATH="$HOME/.local/share/applications/$DESKTOP_FILE_NAME"
ICON_THEME_ROOT="$HOME/.local/share/icons/hicolor"
PIXMAP_ICON="$HOME/.local/share/pixmaps/freqinout.png"

usage() {
  cat <<'EOF'
CommsPlan Linux Uninstaller (legacy filename)

Usage:
  bash uninstall_FreqInOut_linux.sh
  bash uninstall_FreqInOut_linux.sh --dir /path/to/FreqInOut
  INSTALL_DIR=/path/to/FreqInOut bash uninstall_FreqInOut_linux.sh

Options:
  -d, --dir <path>      Install location (default: ~/FreqInOut)
  -y, --yes             Auto-approve prompts
      --dry-run         Show what would be removed without deleting
      --log-file <path> Log output path (default: ~/freqinout-uninstall.log)
  -h, --help            Show help
EOF
}

log() { echo "[CommsPlan] $*"; }
warn() { echo "[CommsPlan] WARNING: $*" >&2; }
die() { echo "[CommsPlan] ERROR: $*" >&2; exit 1; }
expand_path() { local raw="$1"; echo "${raw/#\~/$HOME}"; }
command_exists() { command -v "$1" >/dev/null 2>&1; }

run_cmd() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY RUN: $*"
    return 0
  fi
  "$@"
}

prompt_yes_no() {
  local prompt="$1"
  local answer=""
  if [[ $ASSUME_YES -eq 1 ]]; then
    return 0
  fi
  while true; do
    read -r -p "$prompt [y/N]: " answer || true
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no|"") return 1 ;;
      *) echo "Please enter y or n." ;;
    esac
  done
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -d|--dir)
        [[ $# -ge 2 ]] || die "Missing value for $1"
        INSTALL_DIR="$2"
        shift 2
        ;;
      -y|--yes)
        ASSUME_YES=1
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --log-file)
        [[ $# -ge 2 ]] || die "Missing value for $1"
        LOG_FILE="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown option: $1 (run with --help)"
        ;;
    esac
  done
}

setup_logging() {
  LOG_FILE="$(expand_path "$LOG_FILE")"
  mkdir -p "$(dirname "$LOG_FILE")"
  if command_exists tee; then
    exec > >(tee -a "$LOG_FILE") 2>&1
  fi
  log "Logging to $LOG_FILE"
  [[ $DRY_RUN -eq 1 ]] && log "Dry-run mode enabled; nothing will be deleted."
  return 0
}

integration_belongs_to_install() {
  local target="$1"
  if [[ "$target" == *.desktop ]]; then
    grep -Fxq -- "Path=$INSTALL_DIR" "$target"
  else
    grep -Fxq -- "# CommsPlan installation: $INSTALL_DIR" "$target" ||
      grep -Fxq -- "cd $(printf '%q' "$INSTALL_DIR")" "$target"
  fi
}

remove_desktop_items() {
  local desktop_dir="$HOME/Desktop"
  if command_exists xdg-user-dir; then
    local candidate
    candidate="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    [[ -n "$candidate" ]] && desktop_dir="$candidate"
  fi

  local owns_icons=0
  if [[ -f "$DESKTOP_ENTRY_PATH" ]] && integration_belongs_to_install "$DESKTOP_ENTRY_PATH"; then
    owns_icons=1
    run_cmd rm -f "$DESKTOP_ENTRY_PATH"
  fi
  for command_path in "$LAUNCHER_PATH" "$HOME/.local/bin/commsplan"; do
    if [[ -f "$command_path" ]] && integration_belongs_to_install "$command_path"; then
      run_cmd rm -f "$command_path"
    fi
  done
  local size
  for size in 64 128 256 512 1024; do
    local icon_path="$ICON_THEME_ROOT/${size}x${size}/apps/freqinout.png"
    [[ $owns_icons -eq 1 && -f "$icon_path" ]] && run_cmd rm -f "$icon_path"
  done
  [[ $owns_icons -eq 1 && -f "$PIXMAP_ICON" ]] && run_cmd rm -f "$PIXMAP_ICON"
  if [[ -f "$desktop_dir/$DESKTOP_FILE_NAME" ]] && integration_belongs_to_install "$desktop_dir/$DESKTOP_FILE_NAME"; then
    run_cmd rm -f "$desktop_dir/$DESKTOP_FILE_NAME"
  fi

  if command_exists update-desktop-database; then
    run_cmd update-desktop-database "$HOME/.local/share/applications"
  fi
}

main() {
  parse_args "$@"
  INSTALL_DIR="$(expand_path "$INSTALL_DIR")"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY RUN: would check running processes and ownership, then offer desktop/app removal at $INSTALL_DIR; profile data is retained."
    return
  fi
  setup_logging

  # Fail closed rather than remove a running source/packaged application.
  command_exists python3 || die "Python is required for the running-process check."
  if ! python3 - <<'PROCESS'
import os
from pathlib import Path
proc = Path('/proc')
if not proc.is_dir():
    raise SystemExit('Cannot inspect Linux processes; uninstall stopped.')
for entry in proc.iterdir():
    if not entry.name.isdigit() or int(entry.name) in {os.getpid(), os.getppid()}:
        continue
    try:
        args = [v.decode(errors='replace') for v in (entry / 'cmdline').read_bytes().split(b'\0') if v]
    except FileNotFoundError:
        continue
    except PermissionError:
        raise SystemExit('Cannot inspect all processes; close apps and resolve permissions before uninstall.')
    if not args:
        continue
    name = Path(args[0]).name.lower()
    module = any(a == '-m' and i + 1 < len(args) and args[i+1] == 'freqinout.main' for i,a in enumerate(args))
    if module or name in {'freqinout','commsplan','freqinout.exe','commsplan.exe','freqinout.appimage','commsplan.appimage'}:
        raise SystemExit('Close FIO/CommsPlan before uninstalling.')
PROCESS
  then
    die "Running-process safety check did not pass."
  fi
  for owned_path in "$LAUNCHER_PATH" "$HOME/.local/bin/commsplan" "$DESKTOP_ENTRY_PATH"; do
    if [[ -f "$owned_path" ]] && ! integration_belongs_to_install "$owned_path"; then
      die "Desktop integration belongs to another installation; no files removed."
    fi
  done
  INSTALL_DIR="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$INSTALL_DIR")"
  case "$INSTALL_DIR" in
    /|"$HOME"|"$HOME/Radio") die "Refusing to remove a home/root/container directory." ;;
  esac
  log "Preparing to uninstall CommsPlan."
  log "Install folder: $INSTALL_DIR"

  if ! prompt_yes_no "Remove launcher, desktop icon, and menu entry?"; then
    warn "Skipped desktop item removal."
  else
    remove_desktop_items
  fi

  if [[ -d "$INSTALL_DIR" ]]; then
    if prompt_yes_no "Remove application folder at '$INSTALL_DIR'?"; then
      # A source tree can contain operator data (including historical config).
      # Retain it for manual archival rather than claim the profile was preserved.
      if [[ -e "$INSTALL_DIR/config" || -e "$INSTALL_DIR/runtime" || -e "$INSTALL_DIR/VarAC.ini" ]]; then
        warn "Retained application folder because it contains config/runtime or VarAC.ini. Archive operator data before manual removal."
      else
        run_cmd rm -rf "$INSTALL_DIR"
      fi
    else
      warn "Kept application folder."
    fi
  else
    warn "Install folder not found; skipping app folder removal."
  fi

  log "Uninstall complete."
}

main "$@"
