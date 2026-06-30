#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Gemini Canvas Proxy — VNC Toggle Script
# ──────────────────────────────────────────────────────────────────────────────
# Usage:
#   /app/toggle-vnc.sh start  -> Start x11vnc and noVNC (for logging in / setup)
#   /app/toggle-vnc.sh stop   -> Stop x11vnc and noVNC (saves CPU/RAM during normal use)
# ──────────────────────────────────────────────────────────────────────────────

DISPLAY_NUM="${DISPLAY#:}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
NOVNC_WEB="${NOVNC_WEB:-/usr/share/novnc}"

stop_vnc() {
    echo "[VNC Toggle] Stopping x11vnc and websockify..."
    
    # Find and kill x11vnc
    pkill -f "x11vnc" || true
    # Find and kill websockify
    pkill -f "websockify" || true
    
    echo "[VNC Toggle] VNC server stopped. Idle CPU and RAM resources saved."
}

start_vnc() {
    # Check if x11vnc is already running
    if pgrep -f "x11vnc" >/dev/null; then
        echo "[VNC Toggle] x11vnc is already running."
    else
        echo "[VNC Toggle] Starting x11vnc on display :$DISPLAY_NUM, port $VNC_PORT..."
        x11vnc -display ":$DISPLAY_NUM" \
            -nopw \
            -forever \
            -shared \
            -xkb \
            -rfbport "$VNC_PORT" \
            >/tmp/x11vnc.log 2>&1 &
    fi

    # Check if websockify is already running
    if pgrep -f "websockify" >/dev/null; then
        echo "[VNC Toggle] websockify is already running."
    else
        echo "[VNC Toggle] Starting websockify on port $NOVNC_PORT..."
        websockify --web="$NOVNC_WEB" "$NOVNC_PORT" "localhost:$VNC_PORT" \
            >/tmp/websockify.log 2>&1 &
    fi
    
    echo "[VNC Toggle] VNC server running. Access via: http://<PC2_IP>:$NOVNC_PORT/vnc.html"
}

status_vnc() {
    x11_status="OFFLINE"
    web_status="OFFLINE"
    
    if pgrep -f "x11vnc" >/dev/null; then x11_status="RUNNING"; fi
    if pgrep -f "websockify" >/dev/null; then web_status="RUNNING"; fi
    
    echo "[VNC Toggle] Status:"
    echo "  x11vnc (VNC Server): $x11_status"
    echo "  websockify (noVNC):  $web_status"
}

case "$1" in
    start)
        start_vnc
        ;;
    stop)
        stop_vnc
        ;;
    status)
        status_vnc
        ;;
    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
