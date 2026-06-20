# ⚡ Gemini Canvas Proxy

**Free unlimited Gemini API via Canvas + Chrome extension bridge. No WebSocket, no Local Network Access issues — uses `postMessage` which bypasses Chrome 142+ restrictions entirely.**

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

### 1. Clone & Setup

```bash
git clone https://github.com/pranrichh/gemini-canvas-proxy.git
cd gemini-canvas-proxy
```

**Linux / macOS:**
```bash
chmod +x setup.sh
./setup.sh
```

**Windows (PowerShell):**
```powershell
.\setup.ps1
```

The setup script will:
1. Make the Python host executable
2. Ask for your Chrome extension ID (after you load the extension)
3. Install the native messaging host manifest in all browser config directories

### 2. Load the Extension

1. Open `chrome://extensions/`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder
4. **Copy the Extension ID** (32-character string below the extension name)
5. Run the setup script and paste the ID when prompted

### 3. Start the Canvas Proxy

1. Go to [gemini.google.com](https://gemini.google.com)
2. Tell Gemini: **"Create a web app"**
3. Switch to the **Code** tab
4. Select ALL the generated code → **delete it**
5. Open `canvas-proxy.html` from this project
6. Copy ALL contents → **paste** into the Canvas code editor
7. Click **Preview** — you should see the proxy UI with a green **"Proxy Active"** status

### 4. Test It

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-3-flash-preview",
    "messages": [{"role": "user", "content": "Say hello in 5 words"}]
  }'
```

You should get a standard OpenAI-format response. 🎉

---

## Available Models

The proxy supports any model Canvas is currently promoting. As of June 2026:

| Model ID | Description |
|---|---|
| `gemini-3-flash-preview` | Gemini 3 Flash — fast, capable, great for agents |
| `gemini-2.5-flash-preview-05-20` | Gemini 2.5 Flash Preview |
| `gemini-2.5-pro-preview-04-09` | Gemini 2.5 Pro Preview |
| `gemini-3.1-flash-lite-image-preview` | "Nano Banana 2" — image generation |

Google rotates the promoted model periodically. The Canvas-injected key only works with the currently active model. If you get a 403, try a different model name.

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat completions (supports tools, streaming, multimodal) |
| `/v1/models` | GET | List available models |
| `/health` | GET | Health check |

### Features
- ✅ **Chat completions** — text generation with system prompts
- ✅ **Tool/function calling** — model can generate tool calls; results sent back as text (Canvas key rejects native function role)
- ✅ **Multimodal** — image inputs (data URLs)
- ✅ **Streaming** — faked (single chunk + `[DONE]`)
- ✅ **Multi-turn conversations** — full conversation history
- ✅ **Format translation** — automatic OpenAI ↔ Gemini conversion

### Tool Calling Notes

The Canvas internal key has a limitation: it rejects native `function` and `functionResponse` roles in conversation history (returns 401). The proxy works around this by:
- Converting assistant tool calls to text: `[Calling tool: get_weather({"city": "Tokyo"})]`
- Converting tool results to user messages: `[Tool result from get_weather]: {"weather": "22°C"}`

The model understands these text-encoded tool interactions perfectly. New tool calls (outgoing) use native Gemini function calling — only history is text-encoded.

---

## Integration Guides

### Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is an open-source AI agent framework with full tool calling support.

```bash
# Set the provider to use the proxy
hermes config set model.provider openai
hermes config set model.base_url http://127.0.0.1:8765/v1
hermes config set model.api_key not-needed
hermes config set model.default gemini-3-flash-preview

# Verify it works
hermes chat -q "Say hello"

# Or interactively
hermes
```

Or edit `~/.hermes/config.yaml` directly:
```yaml
model:
  provider: openai
  base_url: http://127.0.0.1:8765/v1
  api_key: not-needed
  default: gemini-3-flash-preview
```

Add the API key to `~/.hermes/.env`:
```
OPENAI_API_KEY=not-needed
```

**Note:** Hermes's tool calling (terminal, browser, file operations) works through the proxy. Tool calls are generated natively by Gemini, executed by Hermes locally, and results are sent back through the proxy.

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
│   ├── gemini_proxy.py        # HTTP server (:8765) + OpenAI↔Gemini translation
│   └── com.gemini.proxy.json  # Native messaging host manifest template
├── setup.sh                   # Setup script (Linux / macOS)
├── setup.ps1                  # Setup script (Windows PowerShell)
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
- Make sure you're on `gemini.google.com` or `canvas.gemini.google.com` with the proxy HTML in the Preview iframe
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

### Tool calling returns 401
- This happens if tool call history contains native `functionCall` parts — the proxy should handle this automatically by converting to text
- Make sure you're running the latest `gemini_proxy.py`

---

## Limitations

- **Canvas tab must stay open** — closing it kills the proxy
- **Model-scoped key** — only the currently promoted model works
- **1MB response limit** — Chrome native messaging host→extension max is 1MB
- **No real streaming** — responses are buffered then sent as a single SSE chunk
- **ToS risk** — using Canvas credentials outside Canvas may violate Google's Terms of Service
- **Tool calling workaround** — function call history is text-encoded (Canvas key rejects native function role in history)

---

## License

MIT

## Credits

- **coxcelot** — [I am canceled autobrowsing agent harness](https://github.com/coxcelot/iamcanceledpresentsagenericautobrowsingagentharness) — the postMessage bridge concept
- **CanvasToAPI** — [iBUHub/CanvasToAPI](https://github.com/iBUHub/CanvasToAPI) — OpenAI ↔ Gemini format translation reference
