#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Gemini Canvas Proxy — Setup Script (Linux / macOS)
# ═══════════════════════════════════════════════════════════════════════════
# This script:
#   1. Makes the Python native host executable
#   2. Asks for the Chrome extension ID (after you load the extension)
#   3. Detects which Chromium browsers are actually installed
#   4. Installs the native messaging host manifest ONLY for installed browsers
#
# Works on: Ubuntu, Debian, Fedora, Arch, macOS, and any Linux distro
# ═══════════════════════════════════════════════════════════════════════════

set -e

# Resolve script directory (handles symlinks)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE_HOST_NAME="com.gemini.proxy"
HOST_SCRIPT="$SCRIPT_DIR/native_host/gemini_proxy.py"
HOST_MANIFEST_TEMPLATE="$SCRIPT_DIR/native_host/com.gemini.proxy.json"

# Sanity check: make sure we're in the right directory
if [ ! -f "$HOST_SCRIPT" ]; then
    echo "✗ ERROR: gemini_proxy.py not found at $HOST_SCRIPT"
    echo "  Make sure you're running this from the gemini-canvas-proxy directory."
    exit 1
fi

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

# ── Step 4: Detect installed browsers and install manifests ──────────────────
# We check for the browser binary in PATH. If found, install to its config dir.
# This avoids creating ghost directories for browsers that aren't installed.

OS_TYPE="$(uname -s)"
INSTALLED_COUNT=0
SKIPPED_BROWSERS=()

install_if_browser_exists() {
    local binary_name="$1"
    local manifest_dir="$2"
    local display_name="$3"

    # Check if the binary exists in PATH, OR if the config dir has a Default profile
    # (some snap/flatpak installs don't put the binary in PATH)
    if command -v "$binary_name" &>/dev/null || [ -d "$manifest_dir/../Default" ] || [ -f "$manifest_dir/../Local State" ]; then
        mkdir -p "$manifest_dir" 2>/dev/null || return
        echo "$GENERATED_MANIFEST" > "$manifest_dir/$NATIVE_HOST_NAME.json"
        INSTALLED_COUNT=$((INSTALLED_COUNT + 1))
        echo "  ✓ $display_name → $manifest_dir"
    else
        SKIPPED_BROWSERS+=("$display_name")
    fi
}

if [ "$OS_TYPE" = "Darwin" ]; then
    # macOS
    install_if_browser_exists "google-chrome" "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts" "Google Chrome"
    install_if_browser_exists "chromium" "$HOME/Library/Application Support/Chromium/NativeMessagingHosts" "Chromium"
    install_if_browser_exists "microsoft-edge" "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts" "Microsoft Edge"
    install_if_browser_exists "brave-browser" "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts" "Brave"
elif [ "$OS_TYPE" = "Linux" ]; then
    install_if_browser_exists "google-chrome" "$HOME/.config/google-chrome/NativeMessagingHosts" "Google Chrome"
    install_if_browser_exists "chromium" "$HOME/.config/chromium/NativeMessagingHosts" "Chromium"
    install_if_browser_exists "chromium-browser" "$HOME/snap/chromium/common/chromium/NativeMessagingHosts" "Chromium (snap)"
    install_if_browser_exists "brave-browser" "$HOME/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts" "Brave"
    install_if_browser_exists "microsoft-edge" "$HOME/.config/microsoft-edge/NativeMessagingHosts" "Microsoft Edge"
    install_if_browser_exists "vivaldi" "$HOME/.config/vivaldi/NativeMessagingHosts" "Vivaldi"
fi

echo ""
if [ "$INSTALLED_COUNT" -eq 0 ]; then
    echo "⚠ No Chromium browsers detected! Install Chrome or Chromium first."
else
    echo "✓ Native messaging manifest installed for $INSTALLED_COUNT browser(s)"
fi

if [ ${#SKIPPED_BROWSERS[@]} -gt 0 ]; then
    echo "  ⊘ Not installed: ${SKIPPED_BROWSERS[*]}"
fi

# ── Step 5: Verify Python is available ──────────────────────────────────────

if command -v python3 &>/dev/null; then
    PYTHON_VER=$(python3 --version 2>&1)
    echo "✓ Python found: $PYTHON_VER"
else
    echo ""
    echo "⚠ python3 not found. The native host requires Python 3.8+."
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
echo "  1. Go to gemini.google.com"
echo "  2. Click the '+' icon (left of the prompt bar)"
echo "  3. Select 'Canvas' from the menu"
echo "  4. Type: Create a web app"
echo "  5. Switch to the Code tab (top of the Canvas panel)"
echo "  6. Select all generated code → delete it"
echo "  7. Open canvas-proxy.html from this project"
echo "  8. Copy ALL contents → paste into Canvas code editor"
echo "  9. Click Preview — you should see '⚡ Gemini Canvas Proxy'"
echo "     with a green 'Proxy Active' status"
echo ""
echo "  10. Test the proxy:"
echo "      curl http://127.0.0.1:8765/v1/chat/completions \\"
echo "        -H 'Content-Type: application/json' \\"
echo "        -d '{\"model\":\"gemini-3-flash-preview\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}'"
echo ""
echo "  11. Use it in any OpenAI-compatible app (see README.md)"
echo ""
