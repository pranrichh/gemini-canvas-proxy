#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Gemini Canvas Proxy — Setup Script (Linux / macOS)
# ═══════════════════════════════════════════════════════════════════════════
# This script:
#   1. Makes the Python native host executable
#   2. Asks for the Chrome extension ID (after you load the extension)
#   3. Installs the native messaging host manifest in ALL possible
#      browser config directories (Chrome, Chromium, Chromium snap, etc.)
#
# Works on: Ubuntu, Debian, Fedora, Arch, macOS, and any Linux distro
# ═══════════════════════════════════════════════════════════════════════════

set -e

# Resolve script directory (handles symlinks)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE_HOST_NAME="com.gemini.proxy"
HOST_SCRIPT="$SCRIPT_DIR/native_host/gemini_proxy.py"
HOST_MANIFEST_TEMPLATE="$SCRIPT_DIR/native_host/com.gemini.proxy.json"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          ⚡  Gemini Canvas Proxy — Setup  ⚡                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Make the Python host executable ──────────────────────────────────

chmod +x "$HOST_SCRIPT"
echo "✓ Native host script is executable"

# ── Step 2: Get the extension ID ─────────────────────────────────────────────

echo ""
echo "━━━ Load the Chrome Extension ━━━"
echo "1. Open chrome://extensions/ (or brave://extensions/, edge://extensions/)"
echo "2. Enable 'Developer mode' (top-right toggle)"
echo "3. Click 'Load unpacked' and select: $SCRIPT_DIR/extension/"
echo "4. Copy the Extension ID (32-char string below the extension name)"
echo ""
read -p "Paste Extension ID: " EXTENSION_ID

if [ -z "$EXTENSION_ID" ]; then
    echo "✗ No extension ID provided. Aborting."
    exit 1
fi

echo ""
echo "✓ Extension ID: $EXTENSION_ID"

# ── Step 3: Generate the native messaging host manifest ─────────────────────

GENERATED_MANIFEST=$(cat << EOF
{
    "name": "$NATIVE_HOST_NAME",
    "description": "Gemini Canvas Proxy — free unlimited LLM API via Canvas postMessage bridge",
    "path": "$HOST_SCRIPT",
    "type": "stdio",
    "allowed_origins": ["chrome-extension://$EXTENSION_ID/"]
}
EOF
)

# ── Step 4: Install manifest in ALL possible locations ──────────────────────
# Different browsers/distros look in different directories. We install
# everywhere to maximize compatibility.

# Detect OS
OS_TYPE="$(uname -s)"

INSTALL_LOCATIONS=()

if [ "$OS_TYPE" = "Darwin" ]; then
    # macOS
    INSTALL_LOCATIONS+=("$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts")
    INSTALL_LOCATIONS+=("$HOME/Library/Application Support/Chromium/NativeMessagingHosts")
    INSTALL_LOCATIONS+=("$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts")
    INSTALL_LOCATIONS+=("$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts")
elif [ "$OS_TYPE" = "Linux" ]; then
    # Linux — Google Chrome
    INSTALL_LOCATIONS+=("$HOME/.config/google-chrome/NativeMessagingHosts")
    # Linux — Chromium (non-snap)
    INSTALL_LOCATIONS+=("$HOME/.config/chromium/NativeMessagingHosts")
    # Linux — Chromium snap (Ubuntu)
    INSTALL_LOCATIONS+=("$HOME/snap/chromium/common/chromium/NativeMessagingHosts")
    # Linux — Brave
    INSTALL_LOCATIONS+=("$HOME/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts")
    # Linux — Microsoft Edge
    INSTALL_LOCATIONS+=("$HOME/.config/microsoft-edge/NativeMessagingHosts")
    # Linux — Vivaldi
    INSTALL_LOCATIONS+=("$HOME/.config/vivaldi/NativeMessagingHosts")
    # System-wide (all browsers)
    INSTALL_LOCATIONS+=("/etc/opt/chrome/native-messaging-hosts")
    INSTALL_LOCATIONS+=("/etc/chromium/native-messaging-hosts")
fi

INSTALLED_COUNT=0
for dir in "${INSTALL_LOCATIONS[@]}"; do
    # Create directory if it doesn't exist (skip system-wide if no permission)
    if mkdir -p "$dir" 2>/dev/null; then
        echo "$GENERATED_MANIFEST" > "$dir/$NATIVE_HOST_NAME.json"
        INSTALLED_COUNT=$((INSTALLED_COUNT + 1))
        echo "  ✓ Installed to: $dir"
    fi
done

echo ""
echo "✓ Native messaging manifest installed in $INSTALLED_COUNT location(s)"

# ── Step 5: Verify Python is available ──────────────────────────────────────

if command -v python3 &>/dev/null; then
    PYTHON_VER=$(python3 --version 2>&1)
    echo "✓ Python found: $PYTHON_VER"
else
    echo "⚠ Warning: python3 not found in PATH. The native host requires Python 3.8+."
    echo "  Install with: sudo apt install python3  (Ubuntu/Debian)"
    echo "                sudo dnf install python3   (Fedora)"
    echo "                brew install python         (macOS)"
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    ✅  Setup Complete  ✅                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo ""
echo "  1. Go to gemini.google.com (or canvas.gemini.google.com)"
echo "  2. Tell Gemini: 'Create a web app'"
echo "  3. Switch to the Code tab"
echo "  4. Select all generated code → delete it"
echo "  5. Open canvas-proxy.html from this project"
echo "  6. Copy ALL contents → paste into Canvas code editor"
echo "  7. Click Preview — you should see '⚡ Gemini Canvas Proxy'"
echo "     with a green 'Proxy Active' status"
echo ""
echo "  8. Test the proxy:"
echo "     curl http://127.0.0.1:8765/v1/chat/completions \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"model\":\"gemini-3-flash-preview\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}'"
echo ""
echo "  9. Use it in any OpenAI-compatible app (see README.md)"
echo ""
