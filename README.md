<h1 align="center">Gemini Canvas Proxy</h1>

<p align="center">
  <img src="https://readme-typing-svg.demolab.com?font=Bitcount&size=25&duration=4000&pause=800&color=FFD700&background=1E40AF&center=true&vCenter=true&width=560&lines=%E2%9C%A6+Free+unlimited+Gemini+API+%E2%9C%A6;%E2%9C%A6+for+any+OpenAI-compatible+app+%E2%9C%A6" alt="typing animation" />
</p>

Turns a free Gemini Canvas session into a local OpenAI-compatible endpoint at `http://127.0.0.1:8765/v1`. Point any OpenAI client at it, use the generated token as the API key.

## Quick start

```bash
git clone https://github.com/pranrichh/gemini-canvas-proxy.git
cd gemini-canvas-proxy
./setup.sh              # Windows: .\setup.ps1
```

1. Load the extension: `chrome://extensions` → Developer mode → Load unpacked → `extension/`. Paste the Extension ID when the setup script asks.
2. Open [gemini.google.com](https://gemini.google.com) → Canvas → "Create an HTML web app" → paste `canvas-proxy.html` into the code view → Preview.
3. Test:

```bash
PROXY_TOKEN="$(cat native_host/.proxy_token)"
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3-flash-preview","messages":[{"role":"user","content":"Hello"}]}'
```

Docker: `docker compose up -d --build`, then noVNC at `http://127.0.0.1:6080`.

## Features

- Chat completions with tool calling, multimodal input, and image generation
- Compatible with Hermes Agent, OpenClaw, LiteLLM, LangChain, the OpenAI SDK, curl
- Bearer auth, loopback-only by default
- Private MessageChannel bridge — no WebSocket, immune to Chrome's Local Network Access restrictions

<p align="center">
  <img src="https://capsule-render.vercel.app/api?type=venom&height=180&color=0:1E40AF,100:3B82F6&text=Docs&fontSize=55&fontColor=FFFFFF" alt="Docs" />
</p>

<p align="center">
  <a href="docs/architecture.md">Architecture</a> · <a href="docs/api.md">API</a> · <a href="docs/deployment.md">Deployment</a> · <a href="docs/security.md">Security</a> · <a href="docs/troubleshooting.md">Troubleshooting</a> · <a href="docs/integrations.md">Integrations</a>
</p>

## License

MIT — using Canvas credentials outside Canvas may violate Google's Terms of Service.
