# Security

The proxy puts an LLM endpoint on your machine, backed by your Google session's
Canvas credentials. Two things can go wrong: someone else reaches the endpoint,
or something you asked the model to look at reaches further than it should.

## What this is not

It is not a multi-tenant service. The bearer token is the only authentication,
there is one user, and nothing is rate limited. If you expose it to a network,
the token is all that stands between a stranger and your Gemini quota.

## HTTP boundary

| Control | Default | Configured by |
|---|---|---|
| Listen address | `127.0.0.1` | `PROXY_BIND` |
| Authentication | required on every `/v1` route and `/status` | `PROXY_TOKEN`, `PROXY_TOKEN_FILE` |
| `/health` | open | — |
| CORS | no `Access-Control-Allow-Origin` unless an origin is named | `PROXY_ALLOWED_ORIGIN` |
| Max request body | 32 MB, refused before the body is read | `PROXY_MAX_REQUEST_BYTES` |
| Token file mode | `0600` | `setup.sh`, `entrypoint.sh` |

Details that matter:

- Token comparison is constant-time (`hmac.compare_digest`). A wrong token gets
  `401` with `WWW-Authenticate`; no token configured at all gets `503`, which is
  a different problem and says so.
- The body limit is enforced against `Content-Length` *before* reading, so a
  large declared length cannot be used to make the process allocate.
- Chunked bodies are read with a running total and cut off at the same limit.
- Error responses close the connection. Without that, an unread request body
  would be parsed as the next request on a keep-alive connection.
- Binding `0.0.0.0` logs a warning at startup, because the bearer token is then
  the only thing between the network and your quota.

## Extension boundary

- The content script matches `https://gemini.google.com/*` and nothing else. No
  `<all_urls>`, no `activeTab`, no `scripting`, no `webRequest`.
- `chrome.runtime.onMessage` checks `sender.tab`, `sender.url`, and the exact
  origin before doing anything. Failures get an explicit refusal, not silence.
- Responses are only accepted from the tab currently registered as the Canvas
  tab. A second Gemini tab cannot answer someone else's request.
- Traffic after the handshake rides a private `MessagePort`. Nothing is
  broadcast to `window` and there is no discoverable endpoint.

The handshake itself is a `postMessage` to the
parent frame, and a sandboxed frame cannot read its parent's origin, so it goes
out with `targetOrigin: "*"`. The receiving content script is what enforces the
policy — it accepts a direct child frame or an opaque-origin (sandboxed) frame
and ignores everything else. A page that could run script in Gemini's own origin
could interfere, but such a page already has your session.

## SSRF: remote images

Fetching an image from a URL the caller supplied turns the proxy into a request
forwarder — the classic path to `http://169.254.169.254/` and to every service
on the local network. It is therefore **off by default**.

With `PROXY_ALLOW_URL_FETCH=true`:

- only `http` and `https` are accepted, and credentials in the URL are refused;
- the hostname is resolved and *every* returned address must be globally
  routable — private, loopback, link-local, reserved, unspecified and multicast
  are all rejected;
- the check runs again on every redirect, so a public URL cannot bounce to a
  private one;
- the response must be `image/*`;
- the body is streamed with a running cap (`PROXY_MAX_IMAGE_BYTES`, 20 MB) and
  a declared `Content-Length` over the cap is refused before reading;
- refusals are logged with a hostname only, and the model sees
  `[Remote image omitted: reason]` rather than a silent gap.

There is a residual DNS-rebinding window: the address is validated, then
`urllib` resolves the name again when it connects. Closing it properly needs a
pinned-IP connection. The feature is opt-in and the proxy is loopback-only by default.

## Canvas-side validation

The Canvas page is the last line, and it is narrow:

- the request path must match
  `/v1beta/models/<model>:(generateContent|streamGenerateContent)`;
- `..` anywhere in the path is refused;
- the URL is built with `new URL(path, origin)` and then the protocol and
  hostname are re-checked, so a path cannot redirect the call;
- the API key is appended with `searchParams`, never concatenated into
  something that could be logged.

The native host validates the model name independently (`^[A-Za-z0-9._-]+$`, no
`..`) and returns `400` before any of it reaches native messaging.

## Operational notes

- Treat `native_host/.proxy_token` and `/browser-data/proxy-token` as secrets.
  They are gitignored. Do not paste them into issues.
- The container prints the token to its log on first start. That is convenient
  and it means `docker compose logs` is sensitive.
- noVNC has no password. Anyone who can reach `:6080` can drive the browser and
  see the session. Keep it on loopback or behind a private network.
- Inside the container the process runs as `proxy` (UID 1000); the bridge socket
  is `0600` in `/tmp`.
- The CI workflow has `contents: read` and runs the stdlib test suite only.
