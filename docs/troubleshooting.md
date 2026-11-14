# Troubleshooting

The first question is always *which link is broken*. The Canvas page answers it:
it shows the page, the extension relay and the native host separately. If two
are lit and one is not, skip to that section.

## Reading the Canvas page

| What it says | What it means |
|---|---|
| Waiting for the extension | the content script has not answered the handshake |
| Extension connected, native host offline | page ⇄ extension works; no Python process behind it |
| Ready | all three links up |

`Waiting for the extension` with the extension installed usually means the tab
was open before the extension was loaded. Reload the tab.

`Native host offline` is the interesting one. It means `connectNative` either
never started a process or the process died. Check
`chrome://extensions` → the extension → **Errors**, and click **service worker**
to open its console; the disconnect reason is logged there.

## `Specified native messaging host not found`

The manifest is missing, in the wrong directory, or names the wrong extension.

```bash
./setup.sh --list          # which browsers it can see
```

Then confirm the manifest exists and points at a real file:

```bash
cat ~/snap/chromium/common/chromium/NativeMessagingHosts/com.gemini.proxy.json
```

The `path` must be absolute and executable. On Ubuntu the Chromium snap reads
from `~/snap/chromium/common/chromium/`, not `~/.config/chromium/` — the setup
script handles both, but a manifest written by an older version of it may be in
the wrong place. Delete the stale one and re-run.

After changing the manifest, restart the browser. Chromium reads the directory
once at startup.

## `Access to the specified native messaging host is forbidden`

The extension ID in the manifest does not match the loaded extension. This
happens when the extension is removed and re-added, because the ID changes.
Re-run `setup.sh` with the new ID.

## The page says "Canvas did not inject an API key"

Canvas rewrites the literal `const apiKey = "";` at compile time. If that line
was edited, reformatted, or the page was pasted into a Canvas that is not using
the HTML renderer, nothing is injected.

Re-paste the current `canvas-proxy.html` without modifying it. When creating the
Canvas, ask for **"Create an HTML web app"** — a plain React Canvas runs the code
through esbuild, which fails on `<!DOCTYPE html>` with
`Expected identifier but found "!"`.

## 401 or 403 from the Gemini API

`401` means the key was rejected: the Canvas session expired, or the page was
re-pasted without the key. Reload the Canvas and re-paste.

`403 unregistered caller` means the key is valid but not for that model. The
Canvas key is scoped to whatever model the session is promoting. Try a different
name from `/v1/models`, or ask the Canvas session which model it is using.

## Empty response with `finish_reason: "length"`

The model hit `max_tokens` before emitting anything. Reasoning models spend
output tokens on internal thinking, and those count against the same budget. If
you set `max_tokens` to 50 for a one-word answer, a thinking model can burn all
50 before it starts answering.

Either raise `max_tokens`, or turn thinking down through the passthrough:

```json
{"model": "gemini-3-flash-preview",
 "max_tokens": 64,
 "extra_body": {"google": {"generation_config": {"thinkingConfig": {"thinkingBudget": 0}}}},
 "messages": [{"role": "user", "content": "Say OK"}]}
```

There is no `reasoning_effort` translation. Gemini does not use that name, so
inventing a mapping would be guessing; `extra_body` passes what you actually
want.

## Empty response or `MALFORMED_FUNCTION_CALL` when tools are present

Gemini rejected part of a tool schema. The proxy strips the keywords Gemini
refuses (`$ref`, `anyOf`, `additionalProperties`, `format`, bounds, and so on),
but a schema with no usable `type` left after stripping can still fail.

Look at the proxy's stderr for the warning naming the function, then simplify
that tool's schema. MCP servers that generate full JSON Schema are the usual
source.

## Tool calls arrive as text

The model wrote `[terminal(command="...")]` into its message instead of emitting
a function call. Two causes:

- the previous assistant turn in the history lost its `thoughtSignature`
  because the client rebuilt the message instead of passing the tool call back
  unchanged;
- the tool schema was rejected, so the model improvised.

Start a fresh conversation and check the proxy log for a
`missing thoughtSignature` warning. If it appears, the client is not
round-tripping `x_gemini_thought_signature`.

## 502 with `error.details`

Either Gemini blocked the prompt (`blockReason`, `finishReason`,
`safetyRatings` are in the details) or there is no bridge:

```bash
curl -s http://127.0.0.1:8765/health
```

`"bridge": {"connected": false}` in Docker means Chromium has not started the
bridge process — usually because `setup-extension.sh` was never run for the
current extension ID, or the container was not restarted after running it.

## 504 Gateway Timeout

The message tells you which case you are in.

*"no request reached the browser"* — the extension is not connected at all. Open
the Canvas page and check the chain.

*"the Canvas page did not answer within Ns"* — the request arrived and nothing
came back. The tab may be closed, throttled, or the model may be very slow. For
long generations use `"stream": true`, which applies the timeout between events
rather than to the whole response, and raise `PROXY_REQUEST_TIMEOUT` if needed.

## Streaming produces nothing, then ends

Check the browser console in the Canvas iframe (right-click inside the preview →
Inspect). The page logs the failed `fetch`. The two common causes are an expired
key (401) and a model that does not support `streamGenerateContent`.

## The container is up but every request 502s

```bash
docker compose exec proxy curl -s localhost:8765/health
docker compose exec proxy tail -n 40 /tmp/proxy.log
docker compose exec proxy ls -l /browser-data/chromium-profile/NativeMessagingHosts
```

If the manifest directory is empty, `setup-extension.sh` was not run. If it is
populated but the bridge is still not connected, Chromium has not restarted
since the manifest was written.

## Login does not survive a container restart

Three things have to hold, and the entrypoint checks two of them at boot:

- the managed policy at `/etc/chromium/policies/managed/gemini-proxy-signin.json`
  exists (the entrypoint warns if not);
- `/browser-data` is a persistent volume, not `down -v`'d;
- Chromium started with `--password-store=basic` (it does).

If all three hold and the login still drops, look at
`docker compose exec proxy tail -50 /tmp/chromium.log` for a profile-write
failure — usually a host bind mount owned by a different UID.

## `setup-extension.sh` rejects the ID

Extension IDs are exactly 32 characters, all in `a`–`p`. Copy it again from
`chrome://extensions`; a partial selection is the usual mistake.

## Diagnosing from the command line

```bash
# is the process up, and does it have a bridge?
curl -s http://127.0.0.1:8765/health | python3 -m json.tool

# full status, including config and in-flight count
curl -s -H "Authorization: Bearer $(cat native_host/.proxy_token)" \
     http://127.0.0.1:8765/status | python3 -m json.tool

# is anything listening?
ss -tlnp | grep 8765

# bare metal: is the host process alive?
pgrep -af gemini_proxy

# docker: proxy stderr, Chromium stderr
docker compose logs -f proxy
docker compose exec proxy tail -n 50 /tmp/chromium.log
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Stdlib only, no network, no browser. They cover translation, the HTTP surface,
streaming, error shapes and chunk reassembly; they do not cover anything that
needs Chrome.

## Known limits

- The Canvas tab must stay open. The credential lives in that page.
- The key is scoped to one model at a time.
- One request in flight per Canvas page. Concurrency comes from having more
  tabs, not from the proxy.
- `n > 1`, `logprobs` and `top_logprobs` are accepted and ignored.
- Driving Canvas from outside the page is outside Google's terms.
