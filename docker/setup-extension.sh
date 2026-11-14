#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Register the native messaging host inside the container.
#
# Chromium reads native messaging manifests from a handful of places; the one
# that needs no system-wide install and no extra flags is
# <user-data-dir>/NativeMessagingHosts/. Since the container's Chromium already
# runs with --user-data-dir=/browser-data/chromium-profile, writing there is
# enough. Restart Chromium afterwards — it reads the directory at startup only.
#
#   docker compose exec proxy /app/setup-extension.sh <extension-id>
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

CHROMIUM_USER_DATA_DIR="${CHROMIUM_USER_DATA_DIR:-/browser-data/chromium-profile}"
NATIVE_HOST_DIR="${NATIVE_HOST_DIR:-$CHROMIUM_USER_DATA_DIR/NativeMessagingHosts}"
MANIFEST_PATH="$NATIVE_HOST_DIR/com.gemini.proxy.json"
HOST_SCRIPT="${HOST_SCRIPT:-/app/native_host/gemini_proxy_bridge.sh}"

EXTENSION_ID="${1:-}"
if [ -z "$EXTENSION_ID" ]; then
    if [ ! -t 0 ]; then
        echo "usage: $0 <extension-id>" >&2
        exit 2
    fi
    printf 'Extension ID: '
    read -r EXTENSION_ID
fi

if ! [[ "$EXTENSION_ID" =~ ^[a-p]{32}$ ]]; then
    echo "error: '$EXTENSION_ID' is not a Chrome extension ID." >&2
    echo "       IDs are exactly 32 characters from a to p." >&2
    exit 1
fi

if [ ! -x "$HOST_SCRIPT" ]; then
    echo "error: $HOST_SCRIPT is missing or not executable." >&2
    echo "       If you bind-mounted an older checkout, rebuild the image." >&2
    exit 1
fi

mkdir -p "$NATIVE_HOST_DIR"
cat > "$MANIFEST_PATH" <<EOF
{
  "name": "com.gemini.proxy",
  "description": "Gemini Canvas Proxy — OpenAI-compatible API backed by a Canvas tab",
  "path": "$HOST_SCRIPT",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXTENSION_ID/"]
}
EOF
chmod 644 "$MANIFEST_PATH"

python3 - "$MANIFEST_PATH" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    json.load(handle)
PY

echo ""
echo "✓ manifest written to $MANIFEST_PATH"
echo "  host:    $HOST_SCRIPT"
echo "  allowed: chrome-extension://$EXTENSION_ID/"
echo ""
echo "Restart so Chromium re-reads it:"
echo "  docker compose restart proxy"
echo ""
echo "Then check:"
echo "  curl -s http://127.0.0.1:8765/health"
