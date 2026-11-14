# Configuration

Every knob is an environment variable. There is no config file, because there is
nothing here that deserves one.

## Proxy

| Variable | Default | Meaning |
|---|---|---|
| `PROXY_PORT` | `8765` | TCP port for the HTTP API |
| `PROXY_BIND` | `127.0.0.1` | listen address; anything else logs a warning |
| `PROXY_TOKEN` | unset | bearer token; overrides the token file |
| `PROXY_TOKEN_FILE` | `native_host/.proxy_token` | where the token is read from when `PROXY_TOKEN` is unset |
| `PROXY_SOCKET` | `/tmp/gemini-canvas-proxy.sock` | Unix socket used to split HTTP and stdio roles |
| `PROXY_REQUEST_TIMEOUT` | `180` | seconds to wait for a non-streaming response, or between stream events |
| `PROXY_STREAM_IDLE_TIMEOUT` | `PROXY_REQUEST_TIMEOUT` | seconds between stream events before giving up |
| `PROXY_MAX_REQUEST_BYTES` | `33554432` (32 MB) | request body ceiling; also capped at 76.8 MB by the chunk protocol |
| `PROXY_TOOL_RESULT_MAX_CHARS` | `5000` | characters of a tool result forwarded to the model before truncation |
| `PROXY_ALLOW_URL_FETCH` | off | allow `http(s)` image URLs; see [security](security.md) |
| `PROXY_MAX_IMAGE_BYTES` | `20971520` (20 MB) | ceiling for a fetched remote image |
| `PROXY_ALLOWED_ORIGIN` | unset | the single origin allowed to make cross-origin requests |
| `PROXY_LOG_FILE` | `native_host/.host.log` | where startup, shutdown and fatal errors are recorded |

Notes on the ones people actually change.

**`PROXY_REQUEST_TIMEOUT`.** The default is generous because a reasoning model
can legitimately take a while. For streaming, the timeout applies between
events: a response that produces a token every 20 seconds is fine no matter how
long it runs overall. Raise it for slow models, lower it if you want failures to
surface quickly.

**`PROXY_TOOL_RESULT_MAX_CHARS`.** Tool output is truncated before it goes into
the conversation, because a 2 MB tool result would otherwise have to be chunked
across native messages and would blow the model's context anyway. The truncation
is marked in the text with the number of characters dropped. Raise it for tools
that legitimately return long structured data.

**`PROXY_MAX_REQUEST_BYTES`.** Two ceilings apply. This one is checked against
`Content-Length` before the body is read. The chunk protocol independently caps
at 128 × 600 KB = 76.8 MB, so setting this higher does not buy you more.

**`PROXY_LOG_FILE`.** Chrome gives a native host a stderr pipe that nothing
reads, so a host that dies on startup leaves no evidence anywhere — the only
visible symptom is the browser restarting it forever. This file records each
start, each exit, and the arguments the process was given. It is the first place
to look when the connection is flapping.

**`PROXY_ALLOWED_ORIGIN`.** The proxy does not emit CORS headers unless you name
an origin, so browsers refuse to call it from a web page by default. Setting
this to, say, `http://localhost:3000` allows that one origin and nothing else.
It is not an authentication mechanism — the bearer token still applies.

## Container

These are read by `entrypoint.sh` and `setup-extension.sh`; the proxy ignores
them.

| Variable | Default | Meaning |
|---|---|---|
| `BROWSER_DATA_DIR` | `/browser-data` | root of everything persistent |
| `CHROMIUM_USER_DATA_DIR` | `/browser-data/chromium-profile` | Chromium profile |
| `NATIVE_HOST_DIR` | `$CHROMIUM_USER_DATA_DIR/NativeMessagingHosts` | where the manifest goes |
| `PROXY_TOKEN_FILE` | `/browser-data/proxy-token` | token location inside the container |
| `NOVNC_PORT` | `6080` | noVNC web UI |
| `VNC_PORT` | `5900` | x11vnc, inside the container only |
| `DISPLAY` | `:99` | X display number |
| `NOVNC_WEB` | `/usr/share/novnc` | static files websockify serves |
| `PYTHON` | `python3` | interpreter used by the bridge wrapper |

`NATIVE_HOST_DIR` has to agree between `entrypoint.sh` and
`setup-extension.sh`, otherwise the manifest is written somewhere Chromium never
looks. Both default to the directory under the Chromium profile, and compose
sets it explicitly so there is no room for drift.

## Client side

Nothing here is configured in a file, but these are the values clients need:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8765/v1` |
| API key | the token from `native_host/.proxy_token` or `/browser-data/proxy-token` |
| Model | any name in `GET /v1/models` that the Canvas session accepts |

See [integrations](integrations.md) for per-client examples.

## Precedence

`PROXY_TOKEN` beats `PROXY_TOKEN_FILE`. If neither yields a non-empty string,
every authenticated route returns `503` rather than accepting anything — a
missing token is a configuration error, not an invitation.

## Changing values

The proxy reads its environment per request, so a restarted process is not
required for the HTTP-side values — but in practice you restart it, because that
is what a deploy does. The container's variables are baked into the entrypoint's
environment; change them in `docker-compose.yml` or the quadlet unit and
recreate the container.
