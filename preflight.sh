#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Gemini Canvas Proxy — root preflight
# ──────────────────────────────────────────────────────────────────────────────
# Runs as root inside the container. Chowns /browser-data so the in-container
# `proxy` user can write to it (relevant when /browser-data is a host bind
# owned by a different UID), then execs the real entrypoint as `proxy` via
# gosu. Tini remains PID 1 the whole time.
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -x

# Chown /browser-data recursively. Cheap on small dirs, expensive on a
# huge Chromium profile — but only on first boot; subsequent runs hit
# the `chown -R` early-out because everything's already proxy:proxy.
if [ -d /browser-data ]; then
    find /browser-data -not -user proxy -exec chown proxy:proxy {} + 2>/dev/null || true
fi

exec gosu proxy /app/entrypoint.sh "$@"