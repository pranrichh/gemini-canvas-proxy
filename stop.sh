#!/usr/bin/env bash
# Stop a bare-metal native host and report whether the port came free.
#
# Chrome normally stops the host itself when the last native port closes. This
# exists for the leftovers: a crashed host, or one started by hand while
# debugging.
set -uo pipefail

PORT="${PROXY_PORT:-8765}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$HERE/native_host/gemini_proxy.py"

# Match the exact script path. `pgrep -f gemini_proxy` would also kill an
# editor with the file open, a test run, or a `tail` on the log.
PIDS="$(pgrep -f -- "$HOST_SCRIPT" 2>/dev/null || true)"

if [ -z "$PIDS" ]; then
    echo "no native host running from $HOST_SCRIPT"
else
    for pid in $PIDS; do
        echo "stopping pid $pid"
        kill "$pid" 2>/dev/null || true
    done
    sleep 1
    for pid in $PIDS; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "pid $pid ignored SIGTERM; sending SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
fi

if command -v ss >/dev/null 2>&1; then
    if ss -tln 2>/dev/null | grep -q ":$PORT "; then
        echo "port $PORT is still in use:"
        ss -tlnp 2>/dev/null | grep ":$PORT " || true
    else
        echo "port $PORT is free"
    fi
fi
