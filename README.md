# ⚡ Gemini Canvas Proxy

**Free unlimited Gemini API via Canvas + Chrome extension bridge. No WebSocket, no Local Network Access issues — uses `postMessage` which bypasses Chrome 142+ restrictions entirely.**

[![Models](https://img.shields.io/badge/models-4%20working-blue)](#available-models)

Provides an OpenAI-compatible API endpoint at `localhost:8765` backed by free unlimited Gemini inference from Gemini Canvas. Works with any OpenAI-compatible tool — [Hermes Agent](https://github.com/NousResearch/hermes-agent), OpenClaw, LiteLLM, LangChain, curl, anything.

---

## How It Works

```
Your App (Hermes, OpenClaw, curl, etc.)
    │
    ├── HTTP POST localhost:8765/v1/chat/completions
    │
    ▼
Native Host (Python, port 8765)           ← OpenAI ↔ Gemini format translation
    │
    ├── stdio (4-byte length + JSON)      ← Chrome native messaging protocol
    │
    ▼
Chrome Extension (service worker)         ← Routes to the Gemini tab
    │
    ├── chrome.tabs.sendMessage
    │
    ▼
Content Script (top-level Gemini page)    ← Relay between extension and iframe
    │
    ├── window.postMessage                ← Works across sandbox boundaries!
    │                                      (NOT a network call — never blocked)
    ▼
Canvas Proxy Page (in sandboxed iframe)   ← fetch() to Gemini API (FREE)
    │
    ├── fetch('https://generativelanguage.googleapis.com/...')
    │   Auth: Canvas auto-injected credentials (unlimited, model-scoped)
    │
    ▼
Response flows back the same path → HTTP response to your app
```

### The Key Insight

**CanvasToAPI** and similar projects use WebSocket (`ws://localhost:port`) to bridge between the Canvas page and a local server. Chrome 142+ [Local Network Access](https://developers.google.com/privacy-sandbox/blog/local-network-access) blocks these connections from sandboxed iframes — requiring users to disable `chrome://flags/#local-network-access-check`, which is disappearing in Chrome 145+.

**This project uses `postMessage` instead** — a browser-level IPC mechanism that works across sandbox boundaries without any network calls. Chrome cannot block it because it's not a network request. This makes the proxy future-proof.

### How Canvas Auth Works

When you write code containing `const apiKey = "";` in Gemini Canvas, Google **auto-injects** the real API key at compile time. This key:
- Has **unlimited quota** (no rate limit, no daily cap)
- Is **model-scoped** (only works with the currently promoted model)
- Is **session-bound** (dies when the Canvas tab closes)
- Works with models like **Gemini 3 Flash**, **Gemini 2.5 Flash**, **Nano Banana 2** (`gemini-3.1-flash-lite-image-preview`), and others as Google rotates them

### Credits

The `postMessage` bridge concept was inspired by **coxcelot**'s ["I am canceled" autobrowsing agent harness](https://github.com/coxcelot/iamcanceledpresentsagenericautobrowsingagentharness) — an autonomous browser agent that runs inside Gemini Canvas. While that project focuses on browser automation, this project strips it down to just the API proxy layer and adds native messaging host integration for system-level access.

The OpenAI ↔ Gemini format translation was informed by [CanvasToAPI](https://github.com/iBUHub/CanvasToAPI).

---

## Quick Start

### Prerequisites
- **Google Chrome** (or Chromium, Brave, Edge — any Chromium browser)
- **Python 3.8+** (for the native messaging host)
- A **Google account** with access to [Gemini](https://gemini.google.com)

### 1. Installation

**Linux / macOS:**
```bash
git clone https://github.com/ShadowPlague21/gemini-canvas-proxy.git
cd gemini-canvas-proxy
./setup.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/ShadowPlague21/gemini-canvas-proxy.git
cd gemini-canvas-proxy
.\setup.ps1
```

**☁️ Running on a VPS or want Docker?**
Use the self-contained Docker Compose stack — see [Docker (Self-Contained)](#docker-self-contained) below.

### 2. Load the Extension

1. Open `chrome://extensions/`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder
4. **Copy the Extension ID** (32-character string below the extension name)
5. Run the setup script and paste the ID when prompted

### 3. Start the Canvas Proxy

1. Go to [gemini.google.com](https://gemini.google.com)
2. Click the **+** icon (left of the prompt bar)
3. Select **Canvas** from the menu
4. Type: **Create an HTML web app**
5. Switch to the **Code** tab (top of the Canvas panel)
6. Select ALL the generated code → **delete it**
7. Open `canvas-proxy.html` from this project
8. Copy ALL contents → **paste** into the Canvas code editor
9. Click **Preview** — you should see the proxy UI with a green **"Proxy Active"** status

### 4. Test It

**Linux / macOS / Git Bash:**
```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [{"role": "user", "content": "Say hello in 5 words"}]
  }'
```

**Windows (PowerShell):**
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8765/v1/chat/completions" -Method Post -Headers @{"Content-Type"="application/json"} -Body '{"model":"gemini-3-flash-preview","messages":[{"role":"user","content":"Say hello in 5 words"}]}'
```

You should get a standard OpenAI-format response. 🎉

---

## Docker (Self-Contained)

For users who want the proxy isolated from their system Python, or running 24/7 on a server, a single-service `docker compose` setup is included that runs the **entire stack in one container**: Xvfb + Openbox + Chromium + x11vnc + noVNC + the Python proxy. You log into Gemini through the noVNC web UI in your own laptop browser — no Chrome install, no `tmux` soup, no Tailscale gymnastics required for local use.

**What's in the container:** virtual display, lightweight window manager, Chromium (persistent profile), VNC server, noVNC web UI on `:6080`, and the OpenAI-compatible HTTP API on `:8765`. Login cookies and the auto-generated native messaging manifest survive container restarts via a named volume (`browser-data`).

**What you do, end-to-end:**

1. `docker compose up -d --build`
2. Open `http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale` in your **laptop's browser** (not the in-container Chromium).
3. In the noVNC window: log in to `gemini.google.com`, navigate to `chrome://extensions`, enable Developer mode, **Load unpacked → `/app/extension`** (the extension folder is bind-mounted read-only from the repo).
4. Copy the Extension ID (32 lowercase characters).
5. From your laptop: `docker compose exec proxy /app/setup-extension.sh <extension-id>` — this writes the native messaging manifest into the persistent volume so Chromium can find the host on next launch.
6. `docker compose restart proxy` (so Chromium re-reads the manifest), then `curl http://127.0.0.1:8765/v1/models` to confirm.

After step 6 the setup is durable: restarts of the container, host reboots, and `docker compose down / up` cycles all preserve your login session and the manifest.

### Local loopback (default)

```bash
docker compose up -d --build
open http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale
```

Both ports bind `127.0.0.1` only — nothing on your LAN can reach them.

### VPS / Tailscale (override)

The `docker-compose.vps.yml` override flips both the noVNC web UI (`6080`) and the proxy HTTP API (`8765`) to `0.0.0.0` so they're reachable via the VPS's Tailscale IP. Combine files with `-f`:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build
# From any tailnet device:
open http://<vps-tailscale-ip>:6080/vnc.html?autoconnect=true&resize=scale
curl http://<vps-tailscale-ip>:8765/v1/models
```

**Security:** the proxy has no authentication, and noVNC has no VNC password by default. Only expose `0.0.0.0:6080` and `0.0.0.0:8765` behind a private mesh (Tailscale, WireGuard, firewall) that restricts both ports to known peers. If you need a VNC password, set `VNC_PASSWORD` and pass `-rfbauth` to x11vnc in `entrypoint.sh`.

### Shared folder with the host (override)

By default the container gets a Docker-managed named volume (`browser-data`) mounted at `/browser-data` for Chromium's profile, the native messaging manifest, and any other browser-side state. To share that directory with the host — so the same folder backs both the in-container Chromium and a host-Chrome you launch separately — use the `docker-compose.shared.yml` override:

```bash
# Default: shares ./browser-data (created next to docker-compose.yml on first run)
docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d --build

# Custom host path
BROWSER_DATA_HOST=~/.gemini-canvas-proxy/browser-data \
  docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d --build
```

Point host-Chrome/Chromium at the same path so it sees the same cookies:

```bash
google-chrome --user-data-dir="$PWD/browser-data" chrome://extensions
```

Combine all three overrides when you want a VPS that's reachable via Tailscale AND has its browser cache on a host path you control:

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.shared.yml \
               -f docker-compose.vps.yml up -d --build
```

### Image notes

- Base: `python:3.12-slim` + Debian's `chromium`, `xvfb`, `x11vnc`, `novnc`, `websockify`, `openbox` packages. ~600 MB on disk after `apt install`.
- Runs as non-root user `proxy` (UID/GID `1000`) inside the container, via `tini` as PID 1 for clean signal propagation.
- Chromium uses `--user-data-dir=/browser-data/chromium-profile` so login cookies + extensions persist across restarts.
- `/dev/shm` is bumped to 1 GB (`shm_size: 1g`) — Chromium without this hits OOM on every page load.
- Stop with `docker compose down`. Wipe everything (cookies, manifest, profile) with `docker compose down -v`. Logs: `docker compose logs -f`.

---

## Available Models

The Canvas-injected key is model-scoped — it only works with models Canvas is currently promoting. Here are the tested models as of June 2026:

| Model ID | Name | Type | Status |
|---|---|---|---|
| `gemini-3-flash-preview` | Gemini 3 Flash | Text + Tools | ✅ Working |
| `gemini-2.5-flash-preview-05-20` | Gemini 2.5 Flash | Text + Tools | ✅ Working |
| `gemini-3.1-flash-image-preview` | Nano Banana 2 | Image Generation | ✅ Working |
| `gemini-2.5-flash-image` | Nano Banana | Image Generation | ✅ Working |
| `gemini-3.5-flash` | Gemini 3.5 Flash | Text | ❌ 403 (not Canvas-scoped) |
| `gemini-3.1-flash-lite` | Gemini 3.1 Flash-Lite | Text | ❌ 403 (not Canvas-scoped) |
| `gemini-3.1-pro-preview` | Gemini 3.1 Pro | Text | ❌ 403 (not Canvas-scoped) |
| `gemini-2.5-pro-preview-04-09` | Gemini 2.5 Pro | Text | ❌ 403 (not Canvas-scoped) |
| `gemini-3-pro-image-preview` | Nano Banana Pro | Image Generation | ❌ 403 (not Canvas-scoped) |

Google rotates the promoted model periodically. If you get a 403, the Canvas key isn't scoped for that model — try another from the working list above.

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat completions (supports tools, streaming, multimodal) |
| `/v1/models` | GET | List available models |
| `/health` | GET | Health check |

### Features
- ✅ **Chat completions** — text generation with system prompts
- ✅ **Tool/function calling** — native Gemini function calling with `thoughtSignature` support; tool results sent back as native `functionResponse` parts
- ✅ **Multimodal input** — images via data URIs (`data:image/png;base64,...`) AND HTTP URLs (fetched server-side)
- ✅ **Image generation** — Nano Banana 2 / Nano Banana output images as markdown data URLs
- ✅ **Streaming** — faked (single chunk + `[DONE]`), correctly emits `tool_calls` deltas with `finish_reason: "tool_calls"`
- ✅ **Multi-turn conversations** — full conversation history
- ✅ **Format translation** — automatic OpenAI ↔ Gemini conversion

### Multimodal Notes

**Input (vision):** Send images as OpenAI-format content arrays:
```json
{
  "model": "gemini-3-flash-preview",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "What's in this image?"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}}
    ]
  }]
}
```
**Both `data:` URIs and `http(s)://` URLs are supported.** URL images are fetched server-side by the native host and converted to `inlineData` (since Canvas can't fetch arbitrary URLs).

**Large payloads (>900KB):** Chrome native messaging limits host→extension messages to 1MB. When a payload exceeds 900KB, the proxy automatically **chunks** it:
1. Native host splits the serialized JSON into 800KB pieces
2. Each chunk is sent as a separate native messaging message (`api_request_chunk`)
3. The extension service worker reassembles the chunks by index
4. The reassembled JSON is parsed and forwarded to Canvas → Gemini API

This works in **all environments** — no HTTP fetch, no localhost network access, no Local Network Access issues. Pure native messaging.

**Output (image generation):** Image models return images as markdown data URLs in the response content:
```
![generated_image](data:image/png;base64,iVBOR...)
```

### Tool Calling Notes

The proxy uses **native Gemini function calling** for both outgoing tool calls and conversation history:

- **Outgoing**: Tool definitions are translated from OpenAI format to Gemini `functionDeclarations` with UPPERCASE type values
- **History**: Assistant tool calls are sent as native `functionCall` parts with a `thoughtSignature` field (required by Gemini 3). Tool results are sent as native `functionResponse` parts with role `"user"`
- **Schema sanitization**: The proxy automatically strips JSON Schema fields that Gemini rejects (`$schema`, `additionalProperties`, `format`, `nullable`, `title`, `items: true`) — this prevents `MALFORMED_FUNCTION_CALL` errors when using tools from MCP servers or other sources that generate strict JSON Schema

---

## Integration Guides

### Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is an open-source AI agent framework with full tool calling support.

#### Option 1: Interactive Setup (Easiest)

1. Open your terminal and run:
   ```bash
   hermes model
   ```
2. Use the arrow keys to scroll down and select:
   - **`Custom (Direct API)`**
3. When prompted, enter:
   - **Base URL**: `http://127.0.0.1:8765/v1`
   - **API Key**: Press **Enter** (not needed)
4. Select one of the available models (e.g., `gemini-3-flash-preview`).

#### Option 2: Manual Config

Add the proxy as a custom provider in your Hermes config file:
* **Linux / macOS**: `~/.hermes/config.yaml`
* **Windows**: `C:\Users\<YourUsername>\.hermes\config.yaml` (or `$env:USERPROFILE\.hermes\config.yaml`)

```yaml
custom_providers:
  - name: "Local (127.0.0.1:8765)"
    base_url: http://127.0.0.1:8765/v1
    model: gemini-3-flash-preview
    api_mode: chat_completions
```

Then use it:
```bash
# One-off test
hermes chat -q "Say hello" --provider "Local (127.0.0.1:8765)" --model gemini-3-flash-preview

# Or set as default provider in config.yaml
```

**Note:** Full tool calling (terminal, browser, file operations, MCP tools) works through the proxy. Tool calls are generated natively by Gemini, executed by Hermes locally, and results are sent back through the proxy using native `functionResponse` parts.

#### Option 3: Remote / Containerized Setup (Docker / Podman on Windows Host)

If you are running Hermes Agent inside a container (e.g., Docker or Podman) or on another machine in your local network, you can route its traffic to the Gemini Canvas Proxy running on your Windows host:

##### 1. Allow Port 8765 through Windows Firewall
By default, the Windows Firewall blocks incoming local network traffic. Run this command in an **Administrator PowerShell** window on your Windows host to allow remote connection to the proxy:
```powershell
New-NetFirewallRule -DisplayName "Allow Gemini Proxy" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow
```

##### 2. Configure Hermes inside the Container / VM
Point the `base_url` to your Windows host's IP address (e.g. `http://<WINDOWS_IP>:8765/v1`):
```yaml
custom_providers:
  - name: "Windows-Gemini-Proxy"
    base_url: "http://<WINDOWS_IP>:8765/v1"
    model: "gemini-3-flash-preview"  # Default main model
    api_mode: "chat_completions"

# Set the active agent model
active_provider: "Windows-Gemini-Proxy"
active_model: "gemini-3-flash-preview"
```

##### 3. Main Agent vs. Auxiliary Models
* **Main Agent (Orchestration & Tools)**: Use `gemini-3-flash-preview`. It is extremely fast, highly capable, and supports the proxy's native tool-calling translator.
* **Auxiliary Models (Image Generation)**: Use `gemini-3.1-flash-image-preview` (Nano Banana 2). Call it directly from custom tools or sub-agents in Hermes:
  ```python
  from openai import OpenAI

  client = OpenAI(
      base_url="http://<WINDOWS_IP>:8765/v1",
      api_key="none"
  )

  response = client.chat.completions.create(
      model="gemini-3.1-flash-image-preview",
      messages=[{"role": "user", "content": "Generate a picture of a futuristic datacenter hosting podman containers"}]
  )
  print(response.choices[0].message.content)
  ```

### OpenClaw / Any OpenAI-Compatible Tool

Point any tool that accepts an OpenAI base URL to `http://127.0.0.1:8765/v1`:

```python
# Python OpenAI SDK
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="not-needed"
)

response = client.chat.completions.create(
    model="gemini-3-flash-preview",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

```javascript
// JavaScript / Node.js
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:8765/v1",
  apiKey: "not-needed",
});

const response = await client.chat.completions.create({
  model: "gemini-3-flash-preview",
  messages: [{ role: "user", content: "Hello!" }],
});
```

```bash
# LangChain
export OPENAI_API_KEY=not-needed
export OPENAI_API_BASE=http://127.0.0.1:8765/v1
```

---

## Project Structure

```
gemini-canvas-proxy/
├── canvas-proxy.html          # Paste into Gemini Canvas code view (React UI)
├── extension/
│   ├── manifest.json          # Chrome MV3 extension manifest
│   ├── background.js          # Service worker: native host ↔ content script
│   └── content_script.js      # PostMessage relay: iframe ↔ extension
├── native_host/
│   └── gemini_proxy.py        # HTTP server (:8765) + OpenAI↔Gemini translation
├── setup.sh                   # Setup script (Linux / macOS)
├── setup.ps1                  # Setup script (Windows PowerShell)
├── stop.sh                    # Stop the proxy (Linux / macOS)
├── Dockerfile                 # Self-contained image (Chromium + noVNC + proxy)
├── docker-compose.yml         # Run the full stack in a container (loopback only)
├── docker-compose.shared.yml  # Override: share browser-data with host
├── docker-compose.vps.yml     # Override: bind 0.0.0.0 for Tailscale/VPS
├── preflight.sh               # Container root preflight (chown + user drop)
├── entrypoint.sh              # Container entrypoint (boots full stack)
├── setup-extension.sh         # In-container extension manifest setup
└── README.md                  # This file
```

---

## How to Use It Daily

1. **Keep the Gemini tab open** — the Canvas tab with the proxy HTML must stay open. Background tab, minimized window, or a separate Chrome profile all work fine.
2. **Reload the extension** if you restart your browser — the native host auto-reconnects.
3. **The proxy auto-starts** when Chrome launches the extension — no manual process management needed.

---

## Troubleshooting

### "No Canvas tab found"
- Make sure you're on `gemini.google.com` with the proxy HTML in the Preview iframe
- Reload the extension at `chrome://extensions/`
- Check the extension's service worker console for debug logs

### "Specified native messaging host not found"
- Re-run the setup script with the correct extension ID
- If using **Chromium snap** (Ubuntu), the manifest must be in `~/snap/chromium/common/chromium/NativeMessagingHosts/` — the setup script handles this automatically
- Verify the `path` in the manifest points to the correct absolute path of `gemini_proxy.py`

### 401 from Gemini API
- The Canvas key may have expired — re-paste the proxy HTML into Canvas
- Try a different model name (the key is model-scoped)
- Make sure `const apiKey = "";` is at the top of the script (Canvas auto-injects the key)

### 403 "unregistered caller"
- The model name in your request doesn't match what Canvas is promoting
- Try `gemini-3-flash-preview`, `gemini-2.5-flash-preview-05-20`, or check Google's current Canvas model

### [ERROR] Expected identifier but found "!"
- **Cause**: You are pasting the `canvas-proxy.html` code into a **JavaScript/React** canvas. Gemini defaults to React for "web apps", and its compiler (`esbuild`) fails when it sees HTML tags like `<!DOCTYPE html>`.
- **Fix**: When starting the Canvas, explicitly tell Gemini to **"Create an HTML web app"**. This ensures Gemini uses the HTML renderer which correctly parses the proxy code.

---

## Limitations

- **Canvas tab must stay open** — closing it kills the proxy
- **Model-scoped key** — only the currently promoted model works
- **Large payloads** — payloads >900KB are automatically chunked into 800KB pieces across multiple native messaging messages (bypasses 1MB limit). No size limit in practice.
- **No real streaming** — responses are buffered then sent as a single SSE chunk (but `tool_calls` are correctly emitted in streaming format with proper `finish_reason`)
- **ToS risk** — using Canvas credentials outside Canvas may violate Google's Terms of Service
- **Tool calling** — uses native Gemini function calling with `thoughtSignature` for history. Tool schemas are automatically sanitized to remove Gemini-incompatible JSON Schema fields

---

## Stopping the Proxy

The native host runs as a subprocess of Chrome — it auto-starts when the extension loads and auto-stops when Chrome closes. To manually stop it:

**Linux / macOS:**
```bash
./stop.sh
```

**Windows:**
```powershell
# Find and kill the Python process
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*gemini_proxy*' } | Stop-Process -Force
```

Or simply reload/disable the extension at `chrome://extensions/`.

---

## License

MIT

## Credits

- **coxcelot** — [I am canceled autobrowsing agent harness](https://github.com/coxcelot/iamcanceledpresentsagenericautobrowsingagentharness) — the postMessage bridge concept
- **CanvasToAPI** — [iBUHub/CanvasToAPI](https://github.com/iBUHub/CanvasToAPI) — OpenAI ↔ Gemini format translation reference
