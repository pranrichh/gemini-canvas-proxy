#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Gemini Canvas Proxy — post-extension-load setup
# ──────────────────────────────────────────────────────────────────────────────
# Run AFTER loading the unpacked extension in the noVNC browser and copying
# the Extension ID. Writes the native messaging manifest into
# $BROWSER_DATA_DIR/NativeMessagingHosts/ so Chromium can find it on next
# launch (it reads the manifest from the user-data-dir's parent dir on
# Linux).
#
# Usage:
#   docker compose exec proxy /app/setup-extension.sh <extension-id>
#   docker compose exec proxy /app/setup-extension.sh  # interactive prompt
#
# After running this once, restart the container (or just relaunch Chromium)
# and the native messaging host will be available to the extension.
# ──────────────────────────────────────────────────────────────────────────────

# ── Setup ─────────────────────────────────────────────────────────────────────
# Determine where Chromium will look for the manifest.
#
# Chromium reads native messaging manifests from one of these locations:
#   1. <user-data-dir>/NativeMessagingHosts/<name>.json  (per-profile)
#   2. A path registered via the --native-messaging-hosts CLI flag
#   3. OS-defined system paths (Linux: /etc/chromium/native-messaging-hosts/)
#
# We use option 1 — write the manifest inside the Chromium user-data-dir so
# it's discovered automatically without extra CLI flags. The setup script
# lets the user override NATIVE_HOST_DIR if they want option 2 or 3.
CHROMIUM_USER_DATA_DIR="${CHROMIUM_USER_DATA_DIR:-/browser-data/chromium-profile}"
NATIVE_HOST_DIR="${NATIVE_HOST_DIR:-$CHROMIUM_USER_DATA_DIR/NativeMessagingHosts}"
MANIFEST_PATH="$NATIVE_HOST_DIR/com.gemini.proxy.json"
HOST_SCRIPT="/app/native_host/gemini_proxy.py"

if [ -n "${1:-}" ]; then
    EXTENSION_ID="$1"
else
    echo "Paste your Extension ID (32 chars):"
    read -r EXTENSION_ID
fi

if [ -z "$EXTENSION_ID" ]; then
    echo "ERROR: no extension ID provided" >&2
    exit 1
fi

if ! [[ "$EXTENSION_ID" =~ ^[a-p]{32}$ ]]; then
    echo "WARNING: extension ID '$EXTENSION_ID' doesn't look like a valid 32-char ID" >&2
    echo "(Chrome extension IDs are 32 chars from [a-p]; got $(echo -n "$EXTENSION_ID" | wc -c) chars)"
    read -rp "Continue anyway? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || exit 1
fi

mkdir -p "$NATIVE_HOST_DIR"

cat > "$MANIFEST_PATH" <<EOF
{
    "name": "com.gemini.proxy",
    "description": "Gemini Canvas Proxy — free unlimited LLM API via Canvas postMessage bridge",
    "path": "$HOST_SCRIPT",
    "type": "stdio",
    "allowed_origins": ["chrome-extension://$EXTENSION_ID/"]
}
EOF

echo ""
echo "✓ Wrote native messaging manifest to $MANIFEST_PATH"
echo "  path:     $HOST_SCRIPT"
echo "  allowed:  chrome-extension://$EXTENSION_ID/"
echo ""
echo "Restart Chromium inside the container to pick up the manifest:"
echo "  docker compose exec proxy pkill -f chromium"
echo "  (the entrypoint's tail will then exit; restart with docker compose up -d)"
echo ""
echo "Then test the proxy:"
echo "  curl http://127.0.0.1:8765/v1/models"