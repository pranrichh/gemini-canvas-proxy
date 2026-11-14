# Gemini Canvas Proxy

An OpenAI-compatible HTTP endpoint backed by a Gemini Canvas tab.

Gemini Canvas runs your HTML in a sandboxed iframe and injects a working API key
into it. This project puts a small page in that iframe, hands a `MessageChannel`
port out to a browser extension, and lets a local Python process drive it. The
result is an endpoint at `http://127.0.0.1:8765/v1` that any OpenAI client can
talk to.

```text
OpenAI client ──HTTP──▶ native host (Python, :8765)
                            │  stdio, 4-byte length + JSON
                            ▼
                       extension service worker
                            │  chrome.tabs.sendMessage
                            ▼
                       content script on the Gemini page
                            │  private MessageChannel port
                            ▼
                       Canvas page in a sandboxed iframe
                            │  fetch() with the injected key
                            ▼
                  generativelanguage.googleapis.com
```

The `MessageChannel` is the part that matters. A sandboxed iframe cannot open a
WebSocket to `localhost` — Chrome's Local Network Access blocks it, and the flag
that used to allow it is going away. A transferred port is not a network
request, so it keeps working.

## What you need

- A Chromium-family browser (Chrome, Chromium, Brave, Edge, Vivaldi). Firefox
  does not support the same native messaging + MV3 combination this uses.
- Python 3.8 or newer on the machine running the proxy.
- A Google account that can open Gemini Canvas.

## Install on your machine

```bash
git clone https://github.com/pranrichh/gemini-canvas-proxy.git
cd gemini-canvas-proxy
./setup.sh
```

`setup.sh` finds your browsers, writes the native-messaging manifest for each
one, generates a bearer token, and prints the token plus a `curl` command with
your paths already filled in. On Windows, run `.\setup.ps1` instead.

Then, in the browser:

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load
   unpacked**, and select the `extension/` directory. Copy the 32-character ID
   it gets.
2. Go to [gemini.google.com](https://gemini.google.com) and start a Canvas with
   **Create an HTML web app**. Switch to the code view, delete what Gemini
   generated, and paste the entire contents of `canvas-proxy.html` into it.
3. Press **Preview**. The page should say **Ready** with all three links in the
   chain lit.

That page is the proxy's control panel: it shows whether the extension and the
native host are actually connected, and logs each request as it arrives.

## Install with Docker

The image runs the whole stack — virtual display, Chromium, noVNC, and the
proxy — so no browser is needed on the host.

```bash
docker compose up -d --build
open http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale
```

Log into Gemini inside that noVNC window, load the unpacked extension from
`/app/extension`, then register it from the host:

```bash
docker compose exec proxy /app/setup-extension.sh <extension-id>
docker compose restart proxy
```

Full details, including the Tailscale and shared-folder variants, are in
[docs/deployment.md](docs/deployment.md).

## Use it

```bash
TOKEN="$(cat native_host/.proxy_token)"     # Docker: docker compose exec -T proxy cat /browser-data/proxy-token

curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3-flash-preview",
       "messages":[{"role":"user","content":"Hello"}]}'
```

Streaming works for real — the response is forwarded as the model produces it,
not buffered and replayed:

```bash
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3-flash-preview","stream":true,
       "messages":[{"role":"user","content":"Count to ten"}]}'
```

Point any OpenAI client at `http://127.0.0.1:8765/v1` with the token as the API
key. [docs/integrations.md](docs/integrations.md) has working examples for the
Python and JavaScript SDKs, LangChain, LiteLLM, and Hermes.

## What works, and what does not

| Capability | Status |
|---|---|
| Chat completions, streaming | works |
| Tool / function calling, including multi-turn history | works |
| Image input (`data:` URIs) | works |
| Image input (`https://` URLs) | opt-in, `PROXY_ALLOW_URL_FETCH=true` |
| Image generation | works, returned as a markdown data URL |
| Structured output (`response_format: json_object` / `json_schema`) | works |
| `n > 1`, `logprobs`, `top_logprobs` | ignored, with a warning on stderr |
| `/v1/completions`, `/v1/embeddings` | not implemented |
| Audio, video, PDF input | not implemented |

## Things that will bite you

**The Canvas tab has to stay open.** The key lives in that page's JavaScript
context. Close the tab and requests fail with a 504 that says so.

**The model name is not a menu.** The key Canvas injects is scoped to whichever
model that session is promoting. `/v1/models` lists the names that have worked;
if one returns 403, the Canvas session is offering something else.

**This is not a supported Google API.** Driving the Canvas session from outside
the page is outside the terms you agreed to when you signed in. Treat it as a
personal tool, not as infrastructure you depend on.

## Documentation

| Document | Contents |
|---|---|
| [Architecture](docs/architecture.md) | every hop, the wire protocol, why the transport is shaped this way |
| [API](docs/api.md) | endpoints, parameters, streaming, error codes |
| [Configuration](docs/configuration.md) | every environment variable, what it does, its default |
| [Deployment](docs/deployment.md) | bare metal, Compose, VPS, shared folder |
| [Podman](docs/podman.md) | quadlet unit for rootless hosts |
| [Security](docs/security.md) | threat model and the controls on each boundary |
| [Troubleshooting](docs/troubleshooting.md) | symptom → cause → fix |

## Project layout

```text
canvas-proxy.html        the page you paste into Canvas
extension/               MV3 service worker + content-script relay
native_host/
  gemini_proxy.py        HTTP server, native messaging host, format translation
  gemini_proxy_bridge.sh container wrapper for the bridge-only role
setup.sh, setup.ps1      per-user install
setup-extension.sh       in-container manifest install
stop.sh, toggle-vnc.sh   operational helpers
Dockerfile               the full stack in one image
docker-compose*.yml      loopback / VPS / shared-folder variants
tests/                   stdlib unit and HTTP tests
docs/                    the documents above
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

40+ tests, no third-party dependencies. They cover the translation layer, the
HTTP surface, real streaming, error shapes, and chunk reassembly.

## Licence

MIT. See [LICENSE](LICENSE).
