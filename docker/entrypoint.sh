#!/bin/sh
# Start a virtual display for headed Chrome, then run the server.
set -eu

if [ "${FLOW_HEADLESS:-0}" != "1" ] && [ -z "${FLOW_NO_XVFB:-}" ]; then
    display_num="${DISPLAY#:}"
    rm -f "/tmp/.X${display_num}-lock" 2>/dev/null || true
    Xvfb "${DISPLAY}" -screen 0 1366x900x24 -nolisten tcp -ac >/tmp/xvfb.log 2>&1 &
    # Wait until the display answers (up to ~5 s).
    i=0
    while [ ! -S "/tmp/.X11-unix/X${display_num}" ] && [ "$i" -lt 50 ]; do
        i=$((i + 1)); sleep 0.1
    done
fi

exec "$@"
