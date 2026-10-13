#!/bin/sh
# Debian maintainer hook: never replace/remove a running frozen station runtime.
set -eu
case "${1:-}" in
    install|upgrade|remove|deconfigure) ;;
    *) exit 0 ;;
esac
if [ ! -d /proc/self ]; then
    echo 'CommsPlan cannot verify active station processes. No package files were changed.' >&2
    exit 1
fi
for process_name in /proc/[0-9]*/comm; do
    [ -r "$process_name" ] || continue
    # A process can exit between the readability check and read; skip that race.
    name=$(cat "$process_name" 2>/dev/null) || continue
    case "$name" in
        CommsPlan|FreqInOut)
            echo 'CommsPlan or FreqInOut is running. Close it normally, then retry this package operation. No application files were changed.' >&2
            exit 1
            ;;
    esac
done
exit 0
