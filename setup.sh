#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Bare-metal setup for Linux and macOS.
#
# Writes the native-messaging manifest for every Chromium-family browser it can
# find, generates the bearer token if one does not exist, and prints the exact
# next steps with your real paths and token filled in.
#
#   ./setup.sh                      interactive
#   ./setup.sh --extension-id ID    non-interactive
#   ./setup.sh --list               show detected browsers, change nothing
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

NATIVE_HOST_NAME="com.gemini.proxy"
MIN_PYTHON="3.8"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$SCRIPT_DIR/native_host/gemini_proxy.py"
WRAPPER="$SCRIPT_DIR/native_host/.host_wrapper.sh"
TOKEN_FILE="$SCRIPT_DIR/native_host/.proxy_token"

EXTENSION_ID=""
LIST_ONLY=0

usage() {
    cat <<'TEXT'
Gemini Canvas Proxy — setup

  ./setup.sh                      interactive
  ./setup.sh --extension-id ID    non-interactive install
  ./setup.sh --list               show detected browsers and exit

Writes NativeMessagingHosts/com.gemini.proxy.json for every browser it detects
(including the Chromium snap) and creates the bearer token at
native_host/.proxy_token if one does not exist.

Environment:
  PYTHON=/path/to/python3         use this interpreter instead of the detected one
TEXT
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --extension-id) EXTENSION_ID="${2:-}"; shift 2 ;;
        --extension-id=*) EXTENSION_ID="${1#*=}"; shift ;;
        --list) LIST_ONLY=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$HOST_SCRIPT" ] || die "native_host/gemini_proxy.py is missing — run this from a full clone"

# Prefer a system interpreter over whatever happens to be first on PATH. Chrome
# launches this process with a minimal environment, and a virtualenv that only
# exists inside one shell is not a stable target for a browser.
find_python() {
    local candidates=()
    [ -n "${PYTHON:-}" ] && candidates+=("$PYTHON")
    candidates+=(/usr/bin/python3 /usr/local/bin/python3)

    local on_path
    on_path="$(command -v python3 2>/dev/null || true)"
    [ -n "$on_path" ] && candidates+=("$on_path")
    on_path="$(command -v python 2>/dev/null || true)"
    [ -n "$on_path" ] && candidates+=("$on_path")

    for candidate in "${candidates[@]}"; do
        [ -x "$candidate" ] || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN="$(find_python)" || die "Python $MIN_PYTHON+ not found.
    Ubuntu/Debian  sudo apt install python3
    Fedora         sudo dnf install python3
    macOS          brew install python"

# ── browser detection ───────────────────────────────────────────────────────

OS="$(uname -s)"

browser_table() {
    if [ "$OS" = "Darwin" ]; then
        cat <<'EOF'
Google Chrome|google-chrome|Library/Application Support/Google/Chrome/NativeMessagingHosts
Chromium|chromium|Library/Application Support/Chromium/NativeMessagingHosts
Microsoft Edge|microsoft-edge|Library/Application Support/Microsoft Edge/NativeMessagingHosts
Brave|brave-browser|Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts
EOF
    else
        cat <<'EOF'
Google Chrome|google-chrome|.config/google-chrome/NativeMessagingHosts
Chromium|chromium|.config/chromium/NativeMessagingHosts
Chromium (snap)|chromium-browser|snap/chromium/common/chromium/NativeMessagingHosts
Brave|brave-browser|.config/BraveSoftware/Brave-Browser/NativeMessagingHosts
Microsoft Edge|microsoft-edge|.config/microsoft-edge/NativeMessagingHosts
Vivaldi|vivaldi|.config/vivaldi/NativeMessagingHosts
EOF
    fi
}

# Present if the binary is on PATH or the profile directory already exists. The
# second test matters for snap and flatpak installs, whose launchers are not
# always visible from a non-login shell.
browser_present() {
    local binary="$1" dir="$2"
    command -v "$binary" >/dev/null 2>&1 && return 0
    [ -d "$dir/../Default" ] && return 0
    [ -f "$dir/../Local State" ] && return 0
    return 1
}

DETECTED=()
while IFS='|' read -r label binary relpath; do
    [ -n "$label" ] || continue
    dir="$HOME/$relpath"
    browser_present "$binary" "$dir" && DETECTED+=("$label|$dir")
done < <(browser_table)

say ""
say "Gemini Canvas Proxy — setup"
say "───────────────────────────"
say ""
say "  python:      $PYTHON_BIN ($("$PYTHON_BIN" -c 'import platform; print(platform.python_version())'))"
say "  native host: $HOST_SCRIPT"
say ""

if [ ${#DETECTED[@]} -eq 0 ]; then
    warn "no Chromium-family browser detected"
else
    for entry in "${DETECTED[@]}"; do
        ok "found ${entry%%|*}"
    done
fi

if [ "$LIST_ONLY" -eq 1 ]; then
    say ""
    say "--list changes nothing. Re-run without it to install."
    exit 0
fi

[ ${#DETECTED[@]} -gt 0 ] || die "nothing to install into; install Chrome or Chromium first"

# ── interpreter wrapper ─────────────────────────────────────────────────────
# Pinning the interpreter is what makes the manifest independent of PATH. A
# shebang of `#!/usr/bin/env python3` resolves against whatever Chrome happens
# to have, which is not necessarily the Python just verified here.

chmod +x "$HOST_SCRIPT" 2>/dev/null || warn "could not chmod +x $HOST_SCRIPT"

cat > "$WRAPPER" <<EOF
#!/bin/sh
# Generated by setup.sh — pins the interpreter verified at install time.
exec "$PYTHON_BIN" "$HOST_SCRIPT" "\$@"
EOF
chmod 755 "$WRAPPER"
ok "wrote interpreter wrapper"

# ── bearer token ────────────────────────────────────────────────────────────

if [ -s "$TOKEN_FILE" ]; then
    PROXY_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
    ok "reusing the existing token"
else
    mkdir -p "$(dirname "$TOKEN_FILE")"
    (umask 077; "$PYTHON_BIN" -c 'import uuid; print(uuid.uuid4())' > "$TOKEN_FILE")
    PROXY_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
    ok "generated a new token"
fi
chmod 600 "$TOKEN_FILE" 2>/dev/null || true
[ -n "$PROXY_TOKEN" ] || die "$TOKEN_FILE is empty; delete it and re-run"

# ── extension id ────────────────────────────────────────────────────────────

valid_id() { [[ "${1:-}" =~ ^[a-p]{32}$ ]]; }

if ! valid_id "$EXTENSION_ID"; then
    [ -t 0 ] || die "--extension-id is required when stdin is not a terminal"
    say ""
    say "Load the extension first:"
    say "  1. open chrome://extensions  (brave://extensions, edge://extensions)"
    say "  2. turn on Developer mode"
    say "  3. Load unpacked → $SCRIPT_DIR/extension"
    say "  4. copy the 32-character ID under the extension name"
    say ""
    for _ in 1 2 3; do
        read -r -p "Extension ID: " EXTENSION_ID || true
        valid_id "$EXTENSION_ID" && break
        warn "not a valid ID — exactly 32 characters, a to p"
        EXTENSION_ID=""
    done
    valid_id "$EXTENSION_ID" || die "no valid extension ID after three attempts"
fi
ok "extension id: $EXTENSION_ID"

# ── manifests ───────────────────────────────────────────────────────────────

say ""
installed=0
for entry in "${DETECTED[@]}"; do
    dir="${entry#*|}"
    mkdir -p "$dir" 2>/dev/null || { warn "cannot create $dir"; continue; }
    cat > "$dir/$NATIVE_HOST_NAME.json" <<EOF
{
  "name": "$NATIVE_HOST_NAME",
  "description": "Gemini Canvas Proxy — OpenAI-compatible API backed by a Canvas tab",
  "path": "$WRAPPER",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXTENSION_ID/"]
}
EOF
    if "$PYTHON_BIN" -c 'import json,sys; json.load(open(sys.argv[1]))' "$dir/$NATIVE_HOST_NAME.json" 2>/dev/null; then
        ok "$dir"
        installed=$((installed + 1))
    else
        warn "$dir — manifest did not parse, removed"
        rm -f "$dir/$NATIVE_HOST_NAME.json"
    fi
done

[ "$installed" -gt 0 ] || die "no manifest installed; nothing will work until one is"

# ── next steps ──────────────────────────────────────────────────────────────

say ""
say "Restart the browser so it reads the manifest, then:"
say ""
say "  1. gemini.google.com → + → Canvas → \"Create an HTML web app\""
say "  2. open the Code tab, select everything, replace it with"
say "     $SCRIPT_DIR/canvas-proxy.html"
say "  3. press Preview"
say "  4. the page should read \"Ready\"; if it says \"native host offline\","
say "     reload the extension at chrome://extensions"
say ""
say "Test it:"
say ""
say "  export PROXY_TOKEN='$PROXY_TOKEN'"
say "  curl -s http://127.0.0.1:8765/v1/chat/completions \\"
say "    -H \"Authorization: Bearer \$PROXY_TOKEN\" \\"
say "    -H 'Content-Type: application/json' \\"
say "    -d '{\"model\":\"gemini-3-flash-preview\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}'"
say ""
say "Token stored at $TOKEN_FILE (mode 600). Clients use it as the API key."
say "Diagnostics: curl -s http://127.0.0.1:8765/health"
say ""
