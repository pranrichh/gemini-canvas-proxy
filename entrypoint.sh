#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Container entrypoint.
#
# Boots, in order: Xvfb, Openbox, Chromium, x11vnc, websockify/noVNC, then the
# proxy's HTTP role. Everything after Xvfb is started in the background; the
# proxy itself is supervised because a container that is up but not serving is
# worse than one that is down.
#
# The native-messaging bridge is NOT started here. Chromium starts it from the
# manifest, which is why this script only runs `--http-only` and the two halves
# meet on a Unix socket.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

PROXY_BIND="${PROXY_BIND:-0.0.0.0}"
PROXY_PORT="${PROXY_PORT:-8765}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
BROWSER_DATA_DIR="${BROWSER_DATA_DIR:-/browser-data}"
CHROMIUM_USER_DATA_DIR="${CHROMIUM_USER_DATA_DIR:-$BROWSER_DATA_DIR/chromium-profile}"
NATIVE_HOST_DIR="${NATIVE_HOST_DIR:-$CHROMIUM_USER_DATA_DIR/NativeMessagingHosts}"
PROXY_TOKEN_FILE="${PROXY_TOKEN_FILE:-$BROWSER_DATA_DIR/proxy-token}"

# Display number: accept ":99", "99" or ":99.0".
DISPLAY_NUM="${DISPLAY:-:99}"
DISPLAY_NUM="${DISPLAY_NUM#:}"
DISPLAY_NUM="${DISPLAY_NUM%%.*}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":$DISPLAY_NUM"

# $HOME is /app, which is a read-only bind mount in Compose. Anything that
# wants to write to a home directory has to go somewhere else.
export HOME="$BROWSER_DATA_DIR/home"

mkdir -p "$CHROMIUM_USER_DATA_DIR" "$NATIVE_HOST_DIR" "$HOME/.config" /tmp/x11

# ── bearer token ─────────────────────────────────────────────────────────────
if [ -z "${PROXY_TOKEN:-}" ] && [ ! -s "$PROXY_TOKEN_FILE" ]; then
    umask 077
    python3 -c 'import uuid; print(uuid.uuid4())' > "$PROXY_TOKEN_FILE"
    chmod 600 "$PROXY_TOKEN_FILE"
fi
export PROXY_TOKEN_FILE

if [ -n "${PROXY_TOKEN:-}" ]; then
    echo "[entrypoint] bearer token supplied through PROXY_TOKEN"
else
    echo "[entrypoint] bearer token: $(tr -d '\r\n' < "$PROXY_TOKEN_FILE")"
    echo "[entrypoint]   (also at $PROXY_TOKEN_FILE; treat it as a secret)"
fi

# ── 1. Xvfb ─────────────────────────────────────────────────────────────────
# A stale lock from a previous run survives `docker compose restart` because
# /tmp is kept, and Xvfb refuses to start while it exists.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true

echo "[entrypoint] Xvfb on :$DISPLAY_NUM"
Xvfb ":$DISPLAY_NUM" -screen 0 1280x720x24 -nolisten tcp -nolisten unix &
XVFB_PID=$!

for _ in $(seq 1 50); do
    if xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
if ! xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: Xvfb did not come up on :$DISPLAY_NUM" >&2
    exit 1
fi

# ── 2. Openbox ──────────────────────────────────────────────────────────────
# Without a window manager, Chromium paints the X root and noVNC has no way to
# reach the tab strip or URL bar.
echo "[entrypoint] Openbox"
openbox >/tmp/openbox.log 2>&1 &
OPENBOX_PID=$!

# ── 3. Chromium ─────────────────────────────────────────────────────────────
# Flags that matter:
#   --user-data-dir          persistent profile: cookies, extensions, manifest
#   --password-store=basic   no gnome-keyring in the container; without it
#                            Chromium can block on an unlock prompt and leave
#                            the profile half-written, which is one of the ways
#                            a working login disappears on restart
#   --no-sandbox             the container is already the isolation boundary
#   --disable-dev-shm-usage  /dev/shm is small in containers
launch_chromium() {
    chromium \
        --no-sandbox \
        --disable-dev-shm-usage \
        --disable-gpu \
        --password-store=basic \
        --window-size=1280,720 \
        --no-first-run \
        --disable-session-crashed-bubble \
        --noerrdialogs \
        --user-data-dir="$CHROMIUM_USER_DATA_DIR" \
        https://gemini.google.com \
        >/tmp/chromium.log 2>&1 &
    CHROMIUM_PID=$!
}

echo "[entrypoint] Chromium (profile: $CHROMIUM_USER_DATA_DIR)"
launch_chromium

if [ ! -f /etc/chromium/policies/managed/gemini-proxy-signin.json ]; then
    echo "[entrypoint] WARNING: managed policy gemini-proxy-signin.json missing."
    echo "[entrypoint]          Login may not survive restarts (BrowserSignin=0)."
fi

# ── 4. x11vnc ───────────────────────────────────────────────────────────────
# Passwordless by design: this is a single-user container. Anything that can
# reach this port can drive the browser, so never expose it without a private
# network in front.
echo "[entrypoint] x11vnc on :$VNC_PORT"
x11vnc -display ":$DISPLAY_NUM" -nopw -forever -shared -xkb \
    -rfbport "$VNC_PORT" >/tmp/x11vnc.log 2>&1 &
X11VNC_PID=$!

# ── 5. noVNC ────────────────────────────────────────────────────────────────
echo "[entrypoint] noVNC on :$NOVNC_PORT"
NOVNC_WEB="${NOVNC_WEB:-/usr/share/novnc}"
websockify --web="$NOVNC_WEB" "$NOVNC_PORT" "localhost:$VNC_PORT" \
    >/tmp/websockify.log 2>&1 &
WEBSOCKIFY_PID=$!

# ── 6. Proxy HTTP role ──────────────────────────────────────────────────────
echo "[entrypoint] proxy HTTP on $PROXY_BIND:$PROXY_PORT"
python3 /app/native_host/gemini_proxy.py --http-only >/tmp/proxy.log 2>&1 &
PROXY_PID=$!

# ── supervision ─────────────────────────────────────────────────────────────
shutdown() {
    echo "[entrypoint] shutting down"
    kill -TERM "$PROXY_PID" "$WEBSOCKIFY_PID" "$X11VNC_PID" "$CHROMIUM_PID" \
        "$OPENBOX_PID" "$XVFB_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 0
}
trap shutdown INT TERM

# A crashed Chromium leaves the container "healthy" while every request fails.
# Restart it, with a short backoff so a crash loop cannot spin the CPU.
(
    restarts=0
    while sleep 10; do
        if ! kill -0 "$CHROMIUM_PID" 2>/dev/null; then
            restarts=$((restarts + 1))
            echo "[entrypoint] Chromium exited; restart #$restarts" >&2
            sleep "$(( restarts < 5 ? restarts * 2 : 30 ))"
            launch_chromium
        fi
        if ! kill -0 "$PROXY_PID" 2>/dev/null; then
            echo "[entrypoint] proxy exited; recycling the container" >&2
            shutdown
        fi
    done
) &

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Gemini Canvas Proxy — stack running"
echo "═══════════════════════════════════════════════════════════════"
echo "  noVNC:          http://127.0.0.1:$NOVNC_PORT/vnc.html?autoconnect=true&resize=scale"
echo "  Proxy API:      http://127.0.0.1:$PROXY_PORT/v1"
echo "  Manifest dir:   $NATIVE_HOST_DIR"
echo ""
echo "  First run, inside the noVNC browser:"
echo "    1. Log in to gemini.google.com"
echo "    2. chrome://extensions → Developer mode → Load unpacked → /app/extension"
echo "    3. Copy the extension ID"
echo "    4. From the host: docker compose exec proxy /app/setup-extension.sh <ID>"
echo "    5. docker compose restart proxy"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Proxy logs are the ones an operator actually wants; surfacing them keeps
# `docker compose logs -f` useful without exec'ing into the container.
tail -F /tmp/proxy.log
