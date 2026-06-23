#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Gemini Canvas Proxy — container entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Boots the full stack in order:
#   1. Xvfb        — virtual display on :99
#   2. Openbox     — minimal window manager so Chromium isn't on a bare root
#   3. Chromium    — driven via noVNC; persistent profile in $BROWSER_DATA_DIR
#   4. x11vnc      — VNC server on :5900 (inside container)
#   5. websockify  — noVNC web UI on $NOVNC_PORT (default 6080)
#   6. gemini_proxy.py — HTTP + native messaging host
#
# The entrypoint tolerates the user NOT having run `setup-extension.sh` yet —
# the proxy starts and listens on :8765 either way, the native messaging
# manifest just won't exist until the extension is loaded.
# ──────────────────────────────────────────────────────────────────────────────

set -e

PROXY_BIND="${PROXY_BIND:-0.0.0.0}"
PROXY_PORT="${PROXY_PORT:-8765}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
DISPLAY_NUM="${DISPLAY#:}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
BROWSER_DATA_DIR="${BROWSER_DATA_DIR:-/browser-data}"
CHROMIUM_USER_DATA_DIR="${CHROMIUM_USER_DATA_DIR:-$BROWSER_DATA_DIR/chromium-profile}"
NATIVE_HOST_DIR="${NATIVE_HOST_DIR:-$BROWSER_DATA_DIR/NativeMessagingHosts}"
# $HOME is /app (read-only bind mount). Use /tmp for any X11/scratch state
# and /browser-data for anything that must survive container restarts.
export HOME="/browser-data/home"
mkdir -p \
    "$CHROMIUM_USER_DATA_DIR" \
    "$NATIVE_HOST_DIR" \
    "$HOME" \
    "$HOME/.config" \
    /tmp/x11

# Note: preflight.sh (called by the Dockerfile ENTRYPOINT) already chowned
# /browser-data as root before exec'ing this script as `proxy`. Don't
# chown here — we'd lack permission on a host-bind mount.

# ── 1. Xvfb ───────────────────────────────────────────────────────────────────
# Virtual 1280x720x24 display. Big enough for the Canvas UI, small enough
# that Chromium doesn't try to enable hardware-accelerated features.
#
# On `docker compose restart` the container's /tmp is preserved, so the
# previous Xvfb's lock file sticks around. Remove it defensively.
echo "[entrypoint] Starting Xvfb on :$DISPLAY_NUM"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true
Xvfb ":$DISPLAY_NUM" -screen 0 1280x720x24 -nolisten tcp -nolisten unix &
XVFB_PID=$!
sleep 1

# Wait for the X server to actually accept connections before continuing —
# `sleep 1` is a guess; the `xdummy` check below is the real wait.
for _ in $(seq 1 30); do
    if xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done

if ! xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: Xvfb failed to start on :$DISPLAY_NUM" >&2
    exit 1
fi

# ── 2. Openbox ────────────────────────────────────────────────────────────────
# Minimal window manager. Without it, Chromium opens fullscreen on the X
# root and there's no way to bring up the URL bar / tabs from noVNC.
echo "[entrypoint] Starting Openbox"
openbox &
OPENBOX_PID=$!

# ── 3. Chromium ──────────────────────────────────────────────────────────────
# Persistent profile dir so login cookies + extensions survive container
# restarts. Shared with the host via the `browser-data` named volume (or
# the host bind when using docker-compose.shared.yml).
#
# Flags explained:
#   --no-sandbox                    container runs as root inside the namespaced
#                                    user; the kernel sandbox doesn't apply
#   --disable-dev-shm-usage         /dev/shm is tiny in containers; avoid OOM
#   --disable-gpu                   no GPU in container; software rendering
#   --window-size=1280,720          match Xvfb resolution
#   --no-first-run                  don't nag about being the default browser
#   --disable-session-crashed-bubble / --noerrdialogs  cleaner first-run UX
#   --start-page=gemini.google.com  one-click path to the user's first action
echo "[entrypoint] Starting Chromium (profile: $CHROMIUM_USER_DATA_DIR)"
chromium \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --window-size=1280,720 \
    --no-first-run \
    --disable-session-crashed-bubble \
    --noerrdialogs \
    --user-data-dir="$CHROMIUM_USER_DATA_DIR" \
    --start-page="https://gemini.google.com" \
    >/tmp/chromium.log 2>&1 &
CHROMIUM_PID=$!

# ── 4. x11vnc ─────────────────────────────────────────────────────────────────
# Passwordless VNC — this is a single-user container. If you're exposing
# the VNC port to a network, set VNC_PASSWORD via an env var or a docker
# secret and add `-rfbauth` here.
echo "[entrypoint] Starting x11vnc on :$VNC_PORT"
x11vnc -display ":$DISPLAY_NUM" \
    -nopw \
    -forever \
    -shared \
    -xkb \
    -rfbport "$VNC_PORT" \
    >/tmp/x11vnc.log 2>&1 &
X11VNC_PID=$!

# ── 5. websockify + noVNC ────────────────────────────────────────────────────
# /usr/share/novnc/vnc.html is the static web UI shipped by the novnc package.
# websockify listens on $NOVNC_PORT and proxies WebSocket → TCP :$VNC_PORT.
echo "[entrypoint] Starting websockify + noVNC on :$NOVNC_PORT"
NOVNC_WEB="${NOVNC_WEB:-/usr/share/novnc}"
websockify --web="$NOVNC_WEB" "$NOVNC_PORT" "localhost:$VNC_PORT" \
    >/tmp/websockify.log 2>&1 &
WEBSOCKIFY_PID=$!

# ── 6. Proxy ──────────────────────────────────────────────────────────────────
# The Python native host runs LAST so Xvfb/x11vnc/websockify are already
# accepting connections when the healthcheck starts polling.
#
# Standalone mode would let the proxy's main() `return` immediately,
# killing the process and the HTTP thread with it. We launch WITHOUT
# --standalone and instead hold stdin open via a long sleep on the write
# end of the pipe, so the proxy's `read_message()` blocks forever and the
# HTTP thread stays alive.
#
# Stdout is captured to /tmp/proxy.log too — the proxy writes a
# `host_ready` JSON message to stdout in non-standalone mode that we don't
# want leaking into `docker compose logs`.
echo "[entrypoint] Starting gemini_proxy.py on $PROXY_BIND:$PROXY_PORT"
( sleep infinity ) | python3 /app/native_host/gemini_proxy.py \
    >/tmp/proxy.log 2>&1 &
PROXY_PID=$!

# ── Trap for clean shutdown ──────────────────────────────────────────────────
shutdown() {
    echo "[entrypoint] Shutting down"
    kill -TERM "$PROXY_PID" "$WEBSOCKIFY_PID" "$X11VNC_PID" \
        "$CHROMIUM_PID" "$OPENBOX_PID" "$XVFB_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 0
}
trap shutdown INT TERM

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Gemini Canvas Proxy — full stack running"
echo "═══════════════════════════════════════════════════════════════"
echo "  noVNC web UI:   http://127.0.0.1:$NOVNC_PORT/vnc.html?autoconnect=true&resize=scale"
echo "  Proxy HTTP API: http://127.0.0.1:$PROXY_PORT/v1/models"
echo "  Native messaging manifest dir: $NATIVE_HOST_DIR"
echo ""
echo "  Next steps (in the noVNC browser):"
echo "    1. Log in to gemini.google.com"
echo "    2. chrome://extensions → enable Developer mode →"
echo "       Load unpacked → /app/extension"
echo "    3. Copy the Extension ID"
echo "    4. From the host: docker compose exec proxy /app/setup-extension.sh <ID>"
echo "       (or exec into the container and run it there)"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Tail proxy logs to stdout so `docker compose logs -f` shows them.
tail -F /tmp/proxy.log