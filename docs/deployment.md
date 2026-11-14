# Deployment

Three ways to run it: directly on your machine, in Docker Compose, or as a
Podman quadlet. They share the same code and the same token format; what differs
is where Chromium lives.

## Bare metal

```bash
git clone https://github.com/pranrichh/gemini-canvas-proxy.git
cd gemini-canvas-proxy
./setup.sh
```

`setup.sh` checks for Python 3.8+, writes the native-messaging manifest into
every browser profile it can find, generates `native_host/.proxy_token`, and
prints your token with a ready-to-run `curl`. Use `--list` to see which browsers
it detected before installing anything, and `--extension-id ID` for a
non-interactive install.

The native host is not a daemon. Chrome starts it when the extension connects
and kills it when the last port closes, so:

- there is nothing to enable at boot;
- nothing runs while the browser is closed;
- `./stop.sh` exists for the leftovers — a crashed host, or one you started by
  hand while debugging.

After a browser restart, reload the extension once at `chrome://extensions`. If
you skip that, the extension's service worker starts before the manifest is
re-read and the first `connectNative` fails.

Windows uses `.\setup.ps1`. It writes a `.bat` shim (native hosts on Windows
must be executables) and registers it under `HKCU`. To stop it, end the
`python.exe` process whose command line contains `gemini_proxy`, or reload the
extension.

## Docker Compose

One container holds the whole stack: Xvfb, Openbox, Chromium, x11vnc, websockify
and the proxy. You log into Gemini through noVNC in your own browser, so no
Chromium is needed on the host.

```bash
docker compose up -d --build
open http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale
```

Both published ports bind `127.0.0.1`, so nothing on your LAN can reach them.

First run, inside the noVNC window:

1. Log in at `gemini.google.com`.
2. `chrome://extensions` → Developer mode → **Load unpacked** → `/app/extension`
   (the repo is bind-mounted at `/app`).
3. Copy the 32-character extension ID.

Then from the host:

```bash
docker compose exec proxy /app/setup-extension.sh <extension-id>
docker compose restart proxy
```

The restart matters: Chromium reads `NativeMessagingHosts/` once at startup.

Finally, open a Canvas, paste `canvas-proxy.html`, and press Preview. Then:

```bash
TOKEN="$(docker compose exec -T proxy cat /browser-data/proxy-token)"
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/health
```

### What survives a restart

| Thing | Where it lives | Survives `down`/`up` | Survives `down -v` |
|---|---|---|---|
| Google login | Chromium profile in `/browser-data` | yes | no |
| Loaded extension | same profile | yes | no |
| Native messaging manifest | `/browser-data/chromium-profile/NativeMessagingHosts` | yes | no |
| Bearer token | `/browser-data/proxy-token` | yes | no |

`docker compose down -v` deletes the volume. Everything above has to be redone.

### Two roles, one port

The entrypoint runs `gemini_proxy.py --http-only`: it owns port 8765 and listens
on `/tmp/gemini-canvas-proxy.sock`. Chromium separately launches
`gemini_proxy_bridge.sh`, which runs `--bridge-only` and pumps stdio to that
socket.

Chromium restarts its native host whenever the
service worker is recycled; if that process also owned the TCP listener, every
restart would drop the port and clients would see connection refused. With the
split, the HTTP side stays up and simply reports `502 bridge unavailable` for
the second or two the bridge is missing.

### VPS and Tailscale

`docker-compose.vps.yml` publishes both ports on all interfaces:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build
curl -H "Authorization: Bearer $TOKEN" http://<tailscale-ip>:8765/v1/models
```

The proxy needs its token; noVNC needs a private network in front of it. It has
no password, and anyone who reaches it can drive the browser and watch the
session. Tailscale, WireGuard, or a firewall rule restricted to known peers —
not a public interface.

### Shared folder with the host

By default `/browser-data` is a named volume. To point it at a host directory
instead — useful when you want to inspect the profile, or reuse the cookies from
a host Chrome launched against the same path:

```bash
docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d --build
BROWSER_DATA_HOST=~/.gemini-canvas-proxy/browser-data \
  docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d --build
```

Overrides stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.shared.yml -f docker-compose.vps.yml up -d
```

### Image facts

- Base `python:3.12-slim` plus Debian's `chromium`, `xvfb`, `x11vnc`, `novnc`,
  `websockify`, `openbox`. About 1.6 GB on disk after the browser stack.
- Runs as `proxy` (UID/GID 1000); `preflight.sh` chowns a host bind mount as root
  first, then drops privileges with `gosu`.
- `shm_size: 1g` in compose. Chromium writes heavily to `/dev/shm` and the 64 MB
  default causes renderer crashes.
- `--password-store=basic` in the Chromium flags. Without it, Chromium looks for
  gnome-keyring, fails, and can leave the profile half-written — one of the ways
  a working login disappears on restart.
- The managed policy at `/etc/chromium/policies/managed/gemini-proxy-signin.json`
  (`BrowserSignin: 0`, `SyncDisabled: true`) stops Google's account reconcilor
  from discarding cookie-only sessions. The entrypoint warns if it is missing.
- A crashed Chromium is restarted by a watchdog in the entrypoint with a backoff,
  so the container does not sit "healthy" with nothing behind it.
- Logs: `docker compose logs -f` tails the proxy's stderr.

## Podman

Rootless hosts use the quadlet unit in `gemini-canvas-proxy.container`.
[docs/podman.md](podman.md) has the full walkthrough, including the VNC toggle
for small VPS instances:

```bash
podman exec gemini-canvas-proxy /app/toggle-vnc.sh stop
podman exec gemini-canvas-proxy /app/toggle-vnc.sh start
podman exec gemini-canvas-proxy /app/toggle-vnc.sh status
```

## Running it day to day

The Canvas tab is the credential. Everything else can come and go.

1. Keep the tab open. Background, minimised, on another workspace — all fine.
   Closed, and requests fail with a 504 that names the cause.
2. After a browser restart, reload the extension once.
3. Watch the Canvas page. It shows three links separately: page, extension,
   native host. Any of them failing has a different fix, and the page says
   which one.
