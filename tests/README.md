# Tests

No network, no browser, no third-party packages.

```bash
python3 -m unittest discover -s tests -v
```

## What is covered

`test_translation.py` — the pure functions.

- OpenAI → Gemini: role mapping, several system messages, consecutive tool
  results merged into one `user` turn, image data URIs, tool schemas sanitized
  and uppercased, every generation parameter, `response_format` variants.
- Gemini → OpenAI: text, inline images, parallel function calls with
  signatures, the whole finish-reason table, blocked prompts.
- Round trip: a tool call's id and signature survive being sent back.
- Chunking: a 2 MB non-ASCII payload reassembles byte for byte and no envelope
  exceeds the 1 MiB native-messaging frame.

`test_http.py` — the real HTTP server, with a stub on the other end of the Unix
socket.

- Auth: missing, wrong, and unconfigured tokens.
- Routing: `/health`, `/status`, `/v1/models`, `/v1/models/{id}`, unknown paths.
- Bodies: invalid JSON, missing body, missing `Content-Length`, chunked
  transfer, oversize.
- Completion: buffered success, blocked prompt with details, bridge error,
  timeout, tool-call history.
- Streaming: delta order, `[DONE]`, `include_usage`, tool-call deltas, error
  before the stream starts.
- Lifecycle: no bridge attached, health reporting the disconnection.

## What is not covered

Anything that needs Chrome: the service worker, the content script, the
MessageChannel handshake, and the Canvas page's fetch. Those are exercised by
hand against a real browser — see `docs/troubleshooting.md` for the diagnostic
commands.

This is a deliberate split. The parts that can be tested without a browser are
tested on every push; the parts that cannot are kept small and are the ones the
Canvas page's own status chain reports on.
