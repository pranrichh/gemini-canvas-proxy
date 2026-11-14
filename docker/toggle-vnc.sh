#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Start or stop the VNC view without touching the browser.
#
# The container always runs Xvfb and Chromium, because those are what make the
# proxy work. x11vnc and websockify only exist so a human can look at the
# browser, so they are the parts worth switching off when nobody is watching.
#
#   toggle-vnc.sh start | stop | status
#
# x11vnc has to run as the same user that owns the X display. `docker exec` and
# `podman exec` default to root, and root gets MIT-SHM BadAccess, so `start`
# re-execs itself through gosu.
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

if [ "$(id -u)" -eq 0 ] && [ "${1:-}" = "start" ] && id proxy >/dev/null 2>&1; then
    export DISPLAY="${DISPLAY:-:99}"
    exec gosu proxy "$0" "$@"
fi

DISPLAY_NUM="${DISPLAY:-:99}"
DISPLAY_NUM="${DISPLAY_NUM#:}"
DISPLAY_NUM="${DISPLAY_NUM%%.*}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":$DISPLAY_NUM"

VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
NOVNC_WEB="${NOVNC_WEB:-/usr/share/novnc}"

running() { pgrep -f "$1" >/dev/null 2>&1; }

# Match on the port too, so another container's x11vnc on a different display
# is not mistaken for ours.
x11vnc_running() { pgrep -f "x11vnc .*-rfbport $VNC_PORT" >/dev/null 2>&1; }
websockify_running() { pgrep -f "websockify .*$NOVNC_PORT" >/dev/null 2>&1; }

stop_vnc() {
    echo "[vnc] stopping"
    pkill -f "x11vnc .*-rfbport $VNC_PORT" 2>/dev/null && echo "[vnc] x11vnc stopped"
    pkill -f "websockify .*$NOVNC_PORT" 2>/dev/null && echo "[vnc] websockify stopped"
    echo "[vnc] the browser keeps running; only the remote view is gone"
}

start_vnc() {
    if x11vnc_running; then
        echo "[vnc] x11vnc already running"
    else
        echo "[vnc] starting x11vnc on :$DISPLAY_NUM port $VNC_PORT"
        x11vnc -display ":$DISPLAY_NUM" -nopw -forever -shared -xkb \
            -rfbport "$VNC_PORT" >/dev/null 2>&1 &
        sleep 0.5
        if ! x11vnc_running; then
            # Shared memory is the usual failure in containers with a small
            # /dev/shm or a uid mismatch; -noshm is slower but always works.
            echo "[vnc] x11vnc did not stay up; retrying with -noshm"
            x11vnc -display ":$DISPLAY_NUM" -nopw -forever -shared -xkb -noshm \
                -rfbport "$VNC_PORT" >/dev/null 2>&1 &
            sleep 0.5
            x11vnc_running || { echo "[vnc] ERROR: x11vnc failed to start" >&2; return 1; }
        fi
    fi

    if websockify_running; then
        echo "[vnc] websockify already running"
    else
        echo "[vnc] starting websockify on $NOVNC_PORT"
        websockify --web="$NOVNC_WEB" "$NOVNC_PORT" "localhost:$VNC_PORT" \
            >/dev/null 2>&1 &
        sleep 0.3
        websockify_running || { echo "[vnc] ERROR: websockify failed to start" >&2; return 1; }
    fi

    echo "[vnc] ready at http://127.0.0.1:$NOVNC_PORT/vnc.html?autoconnect=true&resize=scale"
}

status_vnc() {
    echo "[vnc] x11vnc:    $(x11vnc_running && echo running || echo stopped)"
    echo "[vnc] websockify: $(websockify_running && echo running || echo stopped)"
    echo "[vnc] display:   :$DISPLAY_NUM"
}

case "${1:-}" in
    start) start_vnc ;;
    stop) stop_vnc ;;
    status) status_vnc ;;
    *) echo "usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
