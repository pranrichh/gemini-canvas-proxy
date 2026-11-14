# Architecture

Four processes and one port. This document follows a request from the socket
that accepts it to the API call that answers it, and explains the constraint
that forced each hop.

## The chain

```text
your client
    │  HTTP POST http://127.0.0.1:8765/v1/chat/completions
    ▼
┌───────────────────────────────────────────────────────────────┐
│ native host — native_host/gemini_proxy.py                     │
│   • OpenAI ⇄ Gemini translation                               │
│   • bearer auth, size limits, timeouts                        │
│   • writes framed JSON to stdout                              │
└───────────────────────────────────────────────────────────────┘
    │  stdio: [4-byte native-endian length][UTF-8 JSON]
    ▼
┌───────────────────────────────────────────────────────────────┐
│ extension service worker — extension/background.js            │
│   • owns the native-messaging port                            │
│   • picks the Canvas tab                                      │
│   • reassembles chunked requests                              │
└───────────────────────────────────────────────────────────────┘
    │  chrome.tabs.sendMessage
    ▼
┌───────────────────────────────────────────────────────────────┐
│ content script — extension/content_script.js                  │
│   • runs on https://gemini.google.com                         │
│   • holds one MessagePort to the Canvas frame                 │
│   • answers liveness pings                                    │
└───────────────────────────────────────────────────────────────┘
    │  MessagePort, transferred once during a postMessage handshake
    ▼
┌───────────────────────────────────────────────────────────────┐
│ Canvas page — canvas-proxy.html, in a sandboxed iframe        │
│   • parses Gemini SSE / JSON                                  │
│   • calls the API with the key Canvas injected                │
└───────────────────────────────────────────────────────────────┘
    │  HTTPS, ?key=<injected>
    ▼
generativelanguage.googleapis.com
```

Each arrow exists because of a specific restriction, not by preference:

| Hop | Why it has to be this |
|---|---|
| client → native host | the only part that looks like a normal API |
| native host → service worker | Chrome only lets an extension talk to a binary it launched |
| service worker → content script | service workers cannot touch page frames |
| content script → Canvas page | the Canvas iframe is sandboxed; the port is the only channel that survives it |
| Canvas page → Google | the key only exists inside that page |

## Why a MessageChannel and not a WebSocket

Most projects in this space run a WebSocket from the Canvas page to
`ws://localhost:<port>`. That worked until Chrome 142, where Local Network
Access began blocking connections from sandboxed iframes to local addresses. The
escape hatch was `chrome://flags/#local-network-access-check`, and it is being
removed.

A `MessageChannel` port is browser-level IPC. It is not a network request, so
LNA has nothing to say about it. The page creates a channel, transfers one end
to `window.parent` in a single `postMessage`, and every later message rides the
port. Nothing is broadcast and there is no URL to discover.

The security of this depends on the receiving side. A sandboxed frame cannot
read its parent's origin, so the handshake is posted with `targetOrigin: "*"`;
the content script decides whether to accept the port. It accepts a direct child
frame of the document, or a frame whose origin is the opaque `"null"` — which is
what a sandboxed iframe reports. Anything else is ignored.

## Canvas credentials

When Canvas compiles a page containing the literal

```js
const apiKey = "";
```

it replaces the empty string with a real API key. That key:

- has no per-minute or per-day quota of its own,
- only works for the model the Canvas session is currently using,
- dies when the tab closes.

The page appends it with `URL.searchParams.set('key', apiKey)` — never by string
concatenation, so it cannot leak into a log line — and refuses to call anything
other than `https://generativelanguage.googleapis.com`.

If Canvas stops injecting, `apiKey` stays empty and the page reports "Canvas did
not inject an API key" rather than sending an unauthenticated request that would
fail in a less obvious way later.

## Messages

Native messaging frames everything as a 4-byte native-endian length followed by
UTF-8 JSON. Chrome caps host→browser messages at 1 MiB and browser→host at
64 MiB.

Host → extension:

| Message | Fields | Meaning |
|---|---|---|
| `host_ready` | `port`, `protocol` | the host is listening; the only proof of liveness |
| `api_request` | `id`, `method`, `path`, `body`, `stream`, `stream_options` | one request |
| `api_request_chunk` | `id`, `chunk_index`, `total_chunks`, `chunk_encoding`, `chunk_data` | part of a request too large for one frame |

Extension → host:

| Message | Fields | Meaning |
|---|---|---|
| `api_response` | `id`, `status`, `data`, `error` | a complete, non-streaming result |
| `api_stream_event` | `id`, `event`, plus payload | `start`, `delta`, `tool_call`, `end`, or `error` |

`host_ready` matters more than it looks. A `Port` object from
`chrome.runtime.connectNative()` exists as soon as the call returns, whether or
not Chrome managed to start the binary. Treating the port itself as proof of
life is what made the old status indicator permanently green.

### Requests larger than 1 MiB

The host serializes the request, splits the bytes into 600 KB pieces, and
base64-encodes each piece into an `api_request_chunk`. 600 KB encodes to 800 KB,
which leaves room for the envelope inside the 1 MiB frame. The extension stores
pieces in `chrome.storage.session` (so a suspended worker does not lose them),
reassembles the exact byte stream, decodes UTF-8 strictly, and then handles the
result as a normal `api_request`.

The ceiling is 128 chunks, so the largest expressible request is 76.8 MB. The
HTTP layer rejects anything above `PROXY_MAX_REQUEST_BYTES` (32 MB by default)
long before that.

## Streaming

Streaming is end to end. The page calls `:streamGenerateContent?alt=sse`, reads
the body with a `ReadableStream` reader, splits on `\n\n`, and posts each delta
over the port. The content script forwards deltas to the service worker, which
posts them to the host. The host writes SSE to the client as they arrive.

Two consequences worth knowing:

- The timeout applies between events, not to the whole response. A model that
  thinks for two minutes before its first token is fine as long as it eventually
  starts.
- If the client disconnects mid-stream, the host stops writing and drops the
  request. Nothing is retried behind your back.

Non-streaming requests take the buffered path and are translated by
`gemini_to_openai()`. Streaming responses are formatted directly by the host,
because the pieces already arrive in OpenAI's delta shape.

## Observability

Each hop can only answer for itself, and the failures look alike from outside:

- the relay is gone, so nothing forwards;
- the page is not running, so nothing answers;
- the host is gone, so nothing is listening.

`GET /diagnostics` asks the extension and the page directly and returns both
answers side by side. The extension reports which tab it would route to and
whether that tab's preview completed the handshake; the page reports whether it
holds a port, which protocol it speaks, and whether Canvas substituted a key
into it. A single failed request cannot tell those three apart, and the
alternative is opening a devtools console inside a sandboxed iframe.

`/health` reports three things: up, transport, bridge attached.

## Two deployment shapes

**Bare metal.** Chrome launches `gemini_proxy.py` through the manifest. That
process owns stdio *and* binds port 8765. One process, no socket.

**Container.** Chromium inside the container launches
`gemini_proxy_bridge.sh`, which runs the host with `--bridge-only`: it pumps
stdio to a Unix socket. The entrypoint runs a second copy with `--http-only`,
which owns port 8765 and listens on that socket. The split exists because
Chromium restarts the native host whenever it likes, and the HTTP listener
should not go down with it.

The socket is mode `0600` and lives in `/tmp` inside the container, so it is not
reachable from outside.

## Manifest V3 state

Service workers are suspended aggressively and module globals do not survive.
Two things live in `chrome.storage.session`:

- which tab completed the handshake, so a passively discovered Gemini tab does
  not become the target by accident;
- partial chunk buffers, swept every 30 seconds and expired at 90 seconds, so a
  half-transferred request body cannot sit in storage indefinitely.

The native port is *not* persisted, because it cannot be. When the worker dies,
Chrome kills the host process with it. On restart the worker reconnects and
waits for a fresh `host_ready` before reporting anything as connected.

## Credits

- [coxcelot/iamcanceled…](https://github.com/coxcelot/iamcanceledpresentsagenericautobrowsingagentharness)
  for the postMessage bridge idea.
- [iBUHub/CanvasToAPI](https://github.com/iBUHub/CanvasToAPI) for the original
  OpenAI ⇄ Gemini translation.
