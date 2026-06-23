# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────────────────────
# Gemini Canvas Proxy — full self-contained stack
# ──────────────────────────────────────────────────────────────────────────────
# This image runs EVERYTHING the proxy needs in one container:
#   - Xvfb (virtual display on :99)
#   - Openbox (lightweight window manager so Chromium isn't a bare X root)
#   - Chromium (drives the Canvas session; persistent profile in /browser-data)
#   - x11vnc (VNC server exposing :99)
#   - websockify + noVNC (web UI on :6080 → VNC)
#   - gemini_proxy.py (Python stdlib HTTP + native messaging host)
#
# The user logs into https://gemini.google.com through the noVNC web UI,
# loads the unpacked extension, copies the extension ID, and runs an in-
# container setup step that generates the native messaging manifest with
# the in-container `python3` path. Then the proxy on :8765 is live.
#
# Image base: python:3.12-slim (consistent with the original proxy-only image).
# ~600 MB on disk after `apt install` of the browser stack.
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS runtime

# OCI labels
LABEL org.opencontainers.image.title="gemini-canvas-proxy"
LABEL org.opencontainers.image.description="Self-contained Gemini Canvas Proxy: Chromium + noVNC + native messaging host in one container"
LABEL org.opencontainers.image.source="https://github.com/pranrichh/gemini-canvas-proxy"
LABEL org.opencontainers.image.licenses="MIT"

# Avoid interactive prompts from apt during build
ENV DEBIAN_FRONTEND=noninteractive

# Browser stack. Pinned versions not strictly necessary — the deps below
# match what `chromium --version` from Debian bookworm pulls in.
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Virtual display + window manager
        xvfb \
        openbox \
        # Browser + its runtime deps
        chromium \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        # VNC server + web frontend
        x11vnc \
        novnc \
        websockify \
        # Privilege drop helper — entrypoint runs as root to chown
        # host-bind-mounted /browser-data, then exec's the stack as `proxy`.
        gosu \
        # Utilities
        ca-certificates \
        curl \
        procps \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --set x-www-browser /usr/bin/chromium 2>/dev/null || true

# Non-root user — same UID/GID convention as the original image.
# python:3.12-slim ships a `proxy` group (UID/GID 13) used by libproxy;
# re-use it if it exists, otherwise create it.
RUN (groupadd --system --gid 1000 proxy 2>/dev/null || \
     groupmod -g 1000 proxy) \
    && (useradd --system --uid 1000 --gid proxy --home-dir /app --shell /bin/bash proxy 2>/dev/null || \
        usermod -u 1000 -g proxy -d /app -s /bin/bash proxy) \
    && mkdir -p /app/native_host /browser-data /home/proxy/.config \
    && chown -R proxy:proxy /app /browser-data /home/proxy

# Copy native host + entrypoint scripts. .dockerignore keeps the rest out
# of the build context.
COPY --chown=root:root native_host/ /app/native_host/
COPY --chown=root:root entrypoint.sh /app/entrypoint.sh
COPY --chown=root:root setup-extension.sh /app/setup-extension.sh
COPY --chown=root:root preflight.sh /app/preflight.sh
# chmod 755 not just +x — git checkout / WSL cp can strip group/world bits,
# so a bare `chmod +x` ends up at 0700 and the proxy user (which IS the
# group) can't exec them.
RUN chmod 755 /app/entrypoint.sh /app/setup-extension.sh /app/preflight.sh \
    && chmod 755 /app/native_host/gemini_proxy.py

# Intentionally stay as ROOT in the image — preflight.sh chowns
# /browser-data (potentially a host-bind mount with different ownership)
# and then uses `gosu` to drop to the `proxy` user before launching the
# stack. Keeping USER root lets the chown succeed on first boot.
WORKDIR /app

# Volumes: /browser-data holds Chromium profile + native messaging manifest.
# Stays empty until first run — populated by the entrypoint + user login.
VOLUME ["/browser-data"]

# Ports:
#   6080 → noVNC web UI (loopback-only by default in compose)
#   8765 → proxy HTTP API (loopback-only by default in compose)
EXPOSE 6080 8765

# tini as PID 1 so Xvfb/Chromium/x11vnc get clean SIGTERM propagation.
# preflight.sh runs as root (chowns /browser-data for the host-bind case)
# then drops to proxy: via `gosu proxy /app/entrypoint.sh`.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/preflight.sh"]
CMD []

ENV PROXY_BIND=0.0.0.0 \
    PROXY_PORT=8765 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    NOVNC_PORT=6080 \
    BROWSER_DATA_DIR=/browser-data \
    CHROMIUM_USER_DATA_DIR=/browser-data/chromium-profile