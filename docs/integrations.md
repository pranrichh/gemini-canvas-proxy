# Integrations

Anything that speaks OpenAI chat completions works. Point it at
`http://127.0.0.1:8765/v1` and use the token as the API key.

```bash
# bare metal
export GEMINI_PROXY_TOKEN="$(cat native_host/.proxy_token)"

# docker
export GEMINI_PROXY_TOKEN="$(docker compose exec -T proxy cat /browser-data/proxy-token)"
```

## curl

```bash
curl -s http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $GEMINI_PROXY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3-flash-preview",
       "messages":[{"role":"user","content":"Hello"}]}'
```

Streaming, one line per event:

```bash
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $GEMINI_PROXY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3-flash-preview","stream":true,
       "messages":[{"role":"user","content":"Count to five"}]}'
```

## Opencode

Opencode v2 reads `opencode.jsonc` from the project or `~/.config/opencode/`.
A custom provider needs a `package`, its `settings`, and the models you want in
the picker.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "gemini-canvas": {
      "name": "Gemini Canvas",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8765/v1",
        "apiKey": "{env:GEMINI_PROXY_TOKEN}"
      },
      "models": {
        "gemini-3-flash-preview": {
          "modelID": "gemini-3-flash-preview",
          "name": "Gemini 3 Flash"
        }
      }
    }
  }
}
```

Then `/models` and pick `gemini-canvas/gemini-3-flash-preview`.

On Opencode v1 the same provider is spelled `"provider"` (singular) with `"npm"`
and `"options"` instead of `"package"` and `"settings"`; the endpoint and model
values are unchanged.

## OMP

OMP reads `~/.omp/agent/models.yml`. `openai-completions` is the Chat Completions
transport, which is what this proxy speaks.

```yaml
providers:
  gemini-canvas:
    baseUrl: http://127.0.0.1:8765/v1
    api: openai-completions
    apiKey: GEMINI_PROXY_TOKEN
    models:
      - id: gemini-3-flash-preview
        name: Gemini 3 Flash
        input: [text, image]
        contextWindow: 1000000
        maxTokens: 65536
```

```bash
export GEMINI_PROXY_TOKEN="$(cat native_host/.proxy_token)"
omp models gemini-canvas
omp --model gemini-canvas/gemini-3-flash-preview
```

`apiKey` is read as an environment variable name when one matches, so the secret
stays out of the file.

## Claude Code

Claude Code speaks the Anthropic Messages API, and this proxy speaks OpenAI chat
completions. `ANTHROPIC_BASE_URL` pointed straight at it will 404 on the first
request — the two formats are different, not just different paths.

Put a Messages-to-chat-completions shim in between, then point Claude Code at
the shim:

```bash
# any shim that accepts /v1/messages and forwards to an OpenAI-compatible base
export ANTHROPIC_BASE_URL="http://127.0.0.1:<shim-port>"
export ANTHROPIC_API_KEY="$GEMINI_PROXY_TOKEN"
claude
```

Tools, images and streaming survive the hop as long as the shim translates them;
this proxy's own translation is already tested for all three.

## Hermes Agent

Interactive: `hermes model` → **Custom (Direct API)** → base URL
`http://127.0.0.1:8765/v1`, API key = the token, model
`gemini-3-flash-preview`.

Or in `~/.hermes/config.yaml`:

```yaml
custom_providers:
  - name: "Canvas (local)"
    base_url: http://127.0.0.1:8765/v1
    api_key: "<token>"
    model: gemini-3-flash-preview
    api_mode: chat_completions
```

```bash
hermes chat -q "Say hello" --provider "Canvas (local)" --model gemini-3-flash-preview
```

## OpenAI Python SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key=os.environ["GEMINI_PROXY_TOKEN"],
)

stream = client.chat.completions.create(
    model="gemini-3-flash-preview",
    messages=[{"role": "user", "content": "Count to five"}],
    stream=True,
)
for chunk in stream:
    piece = chunk.choices[0].delta.content
    if piece:
        print(piece, end="", flush=True)
```

Tool calls are plain OpenAI, with one rule: pass the `tool_calls` object back
exactly as you received it, including `x_gemini_thought_signature`, or Gemini
rejects the history.

```python
import json

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]

messages = [{"role": "user", "content": "Weather in Paris?"}]
first = client.chat.completions.create(
    model="gemini-3-flash-preview", messages=messages, tools=tools,
)
assistant = first.choices[0].message
messages.append(assistant.model_dump(exclude_none=True))

call = assistant.tool_calls[0]
messages.append({"role": "tool", "tool_call_id": call.id,
                 "content": json.dumps({"temperature_c": 18})})

second = client.chat.completions.create(
    model="gemini-3-flash-preview", messages=messages, tools=tools,
)
print(second.choices[0].message.content)
```

`assistant.model_dump()` keeps the extra field, which is what makes the second
request valid.

## OpenAI JavaScript SDK

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:8765/v1",
  apiKey: process.env.GEMINI_PROXY_TOKEN,
});

const response = await client.chat.completions.create({
  model: "gemini-3-flash-preview",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(response.choices[0].message.content);
```

## Anything else

The proxy implements `/v1/chat/completions`, `/v1/models` and
`/v1/models/{id}`. A client that needs the old `/v1/completions` text endpoint,
the Responses API, or `/v1/embeddings` will not find it here.
