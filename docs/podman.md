# Podman

Alternative to Compose for rootless, systemd-managed hosts. The image is
identical; only the unit file differs.

## Why the login survives a restart

Three things have to be true at once, and each one has been the cause of a
"login worked yesterday" bug at some point:

1. Chromium runs with a persistent profile (`--user-data-dir` on a volume).
2. The managed policy is present at
   `/etc/chromium/policies/managed/gemini-proxy-signin.json`:
   `BrowserSignin: 0` and `SyncDisabled: true`. Without it, Google's account
   reconcilor can discard a cookie-only session on restart.
3. The volume holding the profile and the native messaging manifest is not
   deleted between runs.

The image ships the policy and the entrypoint warns if it is missing. The rest
is your volume configuration.

## Quadlet

```bash
podman build -t localhost/gemini-canvas-proxy:local .

# The unit uses %h/Ai/gemini-canvas-proxy as an example. Change the Volume=
# paths to where you actually cloned the repo before installing it.
mkdir -p ~/.config/containers/systemd
cp gemini-canvas-proxy.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start gemini-canvas-proxy.service
systemctl --user status gemini-canvas-proxy.service
```

The unit expects two extra files next to the repo:

- `browser-data/` — created on first run if absent;
- `machine-id` — a copy of `/etc/machine-id`. Chromium derives part of its
  profile encryption from it, and a container gets a fresh one on every start
  unless you pin it. Copy it once:

```bash
cp /etc/machine-id ./machine-id
```

Without that file, the login drops on every container recreation.

## Manual `podman run`

```bash
podman run -d \
  --name gemini-canvas-proxy \
  -p 127.0.0.1:6080:6080 -p 127.0.0.1:8765:8765 \
  -v gemini-canvas-proxy-data:/browser-data \
  -v "$PWD":/app:ro,Z \
  -v /etc/machine-id:/etc/machine-id:ro \
  --shm-size=1g \
  localhost/gemini-canvas-proxy:local
```

## First run

1. Open `http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale`.
2. Log in at `gemini.google.com`.
3. `chrome://extensions` → Developer mode → **Load unpacked** → `/app/extension`.
4. Copy the extension ID.
5. Register it, then restart so Chromium re-reads the manifest:

```bash
podman exec gemini-canvas-proxy /app/setup-extension.sh <extension-id>
systemctl --user restart gemini-canvas-proxy.service
```

6. Check it:

```bash
podman exec gemini-canvas-proxy curl -s localhost:8765/health
```

The token is printed at startup and lives at `/browser-data/proxy-token`:

```bash
podman exec gemini-canvas-proxy cat /browser-data/proxy-token
```

## Saving memory

Xvfb and Chromium always run — they are what makes the proxy work. x11vnc and
websockify exist only so a human can look at the browser, so they are the parts
worth switching off on a small instance:

```bash
podman exec gemini-canvas-proxy /app/toggle-vnc.sh stop
podman exec gemini-canvas-proxy /app/toggle-vnc.sh start
podman exec gemini-canvas-proxy /app/toggle-vnc.sh status
```

`start` re-execs itself as the `proxy` user through `gosu`. Running x11vnc as
root against a display owned by another user fails with MIT-SHM BadAccess, and
the script handles that so you do not have to remember it.

## Diagnostics

```bash
podman exec gemini-canvas-proxy ps aux
podman exec gemini-canvas-proxy tail -n 50 /tmp/proxy.log
podman exec gemini-canvas-proxy tail -n 50 /tmp/chromium.log
journalctl --user -u gemini-canvas-proxy.service -n 80 --no-pager
```

## Notes specific to rootless Podman

- The container's `proxy` user is UID 1000. With `:Z` on the bind mounts, Podman
  relabels them for the container, which is why the unit uses it.
- `PublishPort=6080:6080` publishes on all interfaces. Change it to
  `127.0.0.1:6080:6080` in the unit if the host has a public address.
- `MemoryMax=2048M` in the unit is enough for Chromium plus the proxy; drop it to
  1536M on a very small VPS and expect Chromium to be the thing that suffers.
