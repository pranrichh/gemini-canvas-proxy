# Integrations

Any client that speaks OpenAI chat completions works. Point it at
`http://127.0.0.1:8765/v1` and use the token as the API key.

```bash
# bare metal
TOKEN="$(cat native_host/.proxy_token)"

# docker
TOKEN="$(docker compose exec -T proxy cat /browser-data/proxy-token)"
```

## curl

```bash
curl -s http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3-flash-preview",
       "messages":[{"role":"user","content":"Hello"}]}'
```

Streaming, one line per event:

```bash
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3-flash-preview","stream":true,
       "messages":[{"role":"user","content":"Count to five"}]}'
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
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

Tool calling is plain OpenAI. The one thing to preserve is the signature on the
assistant turn — pass the `tool_calls` object back exactly as you received it,
including `x_gemini_thought_signature`, or Gemini rejects the history:

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
messages.append({
    "role": "tool",
    "tool_call_id": call.id,
    "content": json.dumps({"temperature_c": 18}),
})

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

## LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="<token>",
    model="gemini-3-flash-preview",
)
print(llm.invoke("Hello").content)
```

Or with environment variables, which most LangChain wrappers pick up:

```bash
export OPENAI_API_KEY="<token>"
export OPENAI_BASE_URL="http://127.0.0.1:8765/v1"
```

## LiteLLM

```yaml
model_list:
  - model_name: gemini-canvas
    litellm_params:
      model: openai/gemini-3-flash-preview
      api_base: http://127.0.0.1:8765/v1
      api_key: <token>
```

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

Tool calling works end to end: Gemini emits a native function call, Hermes runs
it locally, and the result returns as a `functionResponse`.

## Anything else

The proxy implements `/v1/chat/completions` and `/v1/models`. If a client needs
`/v1/completions` (the old text-completion endpoint) or `/v1/embeddings`, it
will not work — those are not implemented rather than emulated badly.
