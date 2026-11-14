# API

An OpenAI-compatible surface at `http://127.0.0.1:8765`. Everything under
`/v1` needs `Authorization: Bearer <token>`; the token is in
`native_host/.proxy_token` on bare metal and `/browser-data/proxy-token` in
Docker.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/chat/completions` | yes | chat, tools, images, streaming |
| GET | `/v1/models` | yes | names that have worked with a Canvas key |
| GET | `/v1/models/{id}` | yes | one entry from that list |
| GET | `/status` | yes | bridge state, config, in-flight count |
| GET | `/health` | no | liveness plus bridge state, for healthchecks |
| OPTIONS | any | no | CORS preflight, only useful with `PROXY_ALLOWED_ORIGIN` |

`/health` is deliberately unauthenticated and deliberately dull:

```json
{
  "status": "ok",
  "bridge": { "connected": true, "connected_seconds": 412.7 },
  "uptime_seconds": 913.2
}
```

If `bridge.connected` is false, the process is alive but nothing can reach a
model. That distinction is what makes the endpoint worth polling.

## Chat completions

```json
{
  "model": "gemini-3-flash-preview",
  "messages": [
    {"role": "system", "content": "Answer in one sentence."},
    {"role": "user", "content": "What is a MessageChannel?"}
  ]
}
```

The response is a standard OpenAI completion: `id`, `object`, `created`,
`model`, `choices[0].message`, `finish_reason`, `usage`.

### Parameters

| Request field | Becomes | Notes |
|---|---|---|
| `temperature` | `generationConfig.temperature` | |
| `max_tokens`, `max_completion_tokens` | `generationConfig.maxOutputTokens` | `max_completion_tokens` wins if both are set |
| `top_p` | `generationConfig.topP` | |
| `top_k` | `generationConfig.topK` | |
| `stop` | `generationConfig.stopSequences` | a string becomes a one-item list |
| `seed` | `generationConfig.seed` | |
| `frequency_penalty` | `generationConfig.frequencyPenalty` | |
| `presence_penalty` | `generationConfig.presencePenalty` | |
| `response_format` | `responseMimeType` (+ `responseSchema`) | `json_object` and `json_schema` |
| `tools` | `tools[].functionDeclarations` | schemas sanitized, types uppercased |
| `stream` | `:streamGenerateContent?alt=stream` | see below |
| `stream_options.include_usage` | final chunk carries `usage` | |
| `n > 1` | dropped | Gemini returns one candidate; a warning is logged |
| `logprobs`, `top_logprobs` | dropped | not exposed by the Canvas key |

Anything you know Gemini accepts but OpenAI does not name can be passed through
`extra_body.google.generation_config`, and it is merged into
`generationConfig` verbatim:

```json
{"extra_body": {"google": {"generation_config": {"thinkingConfig": {"thinkingBudget": 0}}}}}
```

That is the supported way to reach thinking controls. There is no invented
mapping for `reasoning_effort`; the OpenAI name does not correspond to anything
Gemini calls the same thing.

### Streaming

```json
{"model": "gemini-3-flash-preview", "stream": true,
 "messages": [{"role": "user", "content": "Count to five"}]}
```

`Content-Type: text/event-stream`. Each event is a `chat.completion.chunk`:

```text
data: {"id":"chatcmpl-e2…","object":"chat.completion.chunk","created":1731…,
       "model":"gemini-3-flash-preview",
       "choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {…"delta":{"content":"One"}…}
data: {…"delta":{"content":", two"}…}
data: {…"delta":{},"finish_reason":"stop"}
data: [DONE]
```

Tool calls arrive as `delta.tool_calls` with `index`, `id`, `function.name` and
`function.arguments`, followed by a chunk with `finish_reason: "tool_calls"`.
With `stream_options.include_usage`, one extra chunk carries `usage` and an
empty `choices` array before `[DONE]`.

This is a real stream: bytes leave the proxy as Gemini produces them. There is
no buffering step and no replay.

### Finish reasons

| Gemini | OpenAI |
|---|---|
| `STOP` | `stop` |
| `MAX_TOKENS` | `length` |
| `SAFETY`, `RECITATION`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`, `IMAGE_SAFETY`, `LANGUAGE` | `content_filter` |
| `OTHER`, `MALFORMED_FUNCTION_CALL`, `UNEXPECTED_TOOL_CALL` | `stop` |
| `functionCall` present | `tool_calls` |

An unrecognised reason maps to `stop` and logs a warning naming it.

A blocked prompt is not an empty success. Gemini returning no candidates is
surfaced as `502` with the reason preserved:

```json
{"error": {"message": "Gemini returned no candidates (block reason: SAFETY)",
           "type": "proxy_error", "code": 502,
           "details": {"blockReason": "SAFETY", "safetyRatings": [...]}}}
```

## Images in

```json
{
  "model": "gemini-3-flash-preview",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "What is this?"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR…"}}
    ]
  }]
}
```

`data:` URIs work out of the box. `http(s)://` URLs are refused unless
`PROXY_ALLOW_URL_FETCH=true`, because fetching a URL the caller chose turns the
proxy into a request forwarder. When enabled, the host resolves the hostname,
rejects anything that is not a public address, re-checks on every redirect,
requires an `image/*` content type, and caps the body at 20 MB. A refused image
becomes a `[Remote image omitted: reason]` text part so the model can see that
something was dropped.

`file` and `input_file` parts are refused with a 400. Inline the content or send
an image.

## Images out

Image models return a markdown data URL in `message.content`:

```text
![generated_image](data:image/png;base64,iVBOR…)
```

It is a data URL rather than a file so the response stays a single JSON object
with no side channel to clean up.

## Tools

OpenAI `tools` become Gemini `functionDeclarations` with uppercase type names.
Two things are handled that are easy to get wrong:

**Schemas are sanitized.** Gemini accepts a strict subset of JSON Schema and
returns `MALFORMED_FUNCTION_CALL` — which reaches the caller as an empty
response — for anything outside it. `$schema`, `$ref`, `additionalProperties`,
`anyOf`/`oneOf`/`allOf`, `format`, `default`, `pattern`, numeric and length
bounds, `title` and the rest are stripped recursively. `items: true` becomes
`items: {}`.

**Signatures round-trip.** Gemini 3 requires a `thoughtSignature` on every
`functionCall` in history. Responses carry it as
`tool_calls[].x_gemini_thought_signature`; sending that structure back in the
next request preserves it. A call without a signature gets a placeholder and a
logged warning — usually a sign that a client rebuilt the message instead of
passing it through.

Consecutive `tool` messages are merged into a single `user` turn, because Gemini
requires alternating roles and rejects two `user` turns in a row.

## Large requests

Above roughly 900 KB the host chunks. You do not have to do anything; the
threshold and the reassembly are internal. The practical ceiling is
`PROXY_MAX_REQUEST_BYTES` (32 MB default), and the hard ceiling imposed by the
chunk count is 76.8 MB.

## Errors

| Status | When |
|---|---|
| `400` | body is not JSON, not an object, has no messages array, bad model name, malformed data URI |
| `401` | missing or wrong bearer token, with `WWW-Authenticate` |
| `403` | CORS preflight from an origin that is not `PROXY_ALLOWED_ORIGIN` |
| `404` | unknown path or unknown model id, as JSON |
| `411` | no `Content-Length` and no chunked encoding |
| `413` | body above `PROXY_MAX_REQUEST_BYTES` |
| `500` | the proxy itself is misconfigured, e.g. `PROXY_PORT` is not a number |
| `502` | Gemini blocked the response, the bridge is gone, or the Canvas page errored |
| `503` | no bearer token is configured at all |
| `504` | nothing answered within `PROXY_REQUEST_TIMEOUT` |

Every error body has the same shape:

```json
{"error": {"message": "…", "type": "proxy_error", "code": 504}}
```

with an optional `details` object. A `504` message distinguishes "no request
reached the browser" from "the Canvas page did not answer in time", because
those have different fixes.
