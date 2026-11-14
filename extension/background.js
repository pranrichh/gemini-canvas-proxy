/**
 * Service worker.
 *
 * Three things live here: the native-messaging port, the routing table for
 * in-flight requests, and the chunk buffers that let a >1 MiB request cross
 * Chrome's native-messaging frame limit.
 *
 * The subtle part is state. Manifest V3 suspends service workers whenever it
 * feels like it, so anything that must outlive a suspension goes in
 * chrome.storage.session. The native port itself cannot be persisted — Chrome
 * kills the host process when the worker dies — so "connected" is never
 * assumed. It is only true between the host's own `host_ready` message and the
 * port's `onDisconnect`.
 */

const PROTOCOL_VERSION = 2;

const CANVAS_STATE_KEY = 'canvasState';
const CHUNK_KEY_PREFIX = 'chunkBuffer:';
const CHUNK_TTL_MS = 90_000;
const CHUNK_SWEEP_ALARM = 'chunk-buffer-ttl-sweep';
const RECONNECT_DELAY_MS = 2_000;

let nativePort = null;
/** True only after the host says `host_ready`; a Port object proves nothing. */
let hostReady = false;
let hostProtocol = 0;
/** The HTTP port the host reports in `host_ready`; shown in the Canvas page. */
let hostPort = 0;
/** Why the last connection attempt failed, verbatim from Chrome. */
let hostError = '';

/**
 * What happened to the most recent answer from the page: how big it was, whether
 * the native port accepted it, and why not. Surfaced by /diagnostics, because a
 * dropped response is otherwise invisible from every side at once — the page
 * believes it replied, the host sees nothing.
 */
let lastResponse = null;

/**
 * Mirrors chrome.storage.session so the hot path can answer a status question
 * without awaiting storage on every message. Kept in sync by setCanvasState().
 */
let activeTabId = null;

// ── Canvas tab state ────────────────────────────────────────────────────────

function isGeminiUrl(url) {
  try {
    return new URL(url).origin === 'https://gemini.google.com';
  } catch {
    return false;
  }
}

async function getCanvasState() {
  const stored = await chrome.storage.session.get(CANVAS_STATE_KEY);
  const state = stored[CANVAS_STATE_KEY] || { tabId: null, ready: false };
  activeTabId = state.tabId;
  return state;
}

async function setCanvasState(tabId, ready) {
  activeTabId = tabId;
  await chrome.storage.session.set({
    [CANVAS_STATE_KEY]: { tabId, ready: Boolean(ready) }
  });
}

// ── Native messaging ────────────────────────────────────────────────────────

function connectNative() {
  if (nativePort) return;

  try {
    nativePort = chrome.runtime.connectNative('com.gemini.proxy');
  } catch (error) {
    console.error('[proxy] connectNative threw:', error);
    hostReady = false;
    setTimeout(connectNative, RECONNECT_DELAY_MS);
    return;
  }

  nativePort.onMessage.addListener(onNativeMessage);

  nativePort.onDisconnect.addListener(() => {
    const reason = chrome.runtime.lastError?.message;
    // Kept and surfaced in the Canvas page. "Native host has exited" and
    // "Specified native messaging host not found" have completely different
    // causes, and asking a user to open a service-worker console to tell them
    // apart is a bad trade.
    hostError = reason || 'the native host process exited';
    console.warn('[proxy] native host disconnected: ' + hostError);
    nativePort = null;
    // A host that failed to launch never sent host_ready, so the broadcast is
    // already correct; a host that died mid-session needs the transition.
    if (hostReady) broadcastBridgeState(false);
    hostReady = false;
    hostProtocol = 0;
    hostPort = 0;
    setTimeout(connectNative, RECONNECT_DELAY_MS);
  });
}

function onNativeMessage(message) {
  if (message.type === 'host_ready') {
    hostReady = true;
    hostProtocol = message.protocol || 0;
    hostPort = message.port || 0;
    hostError = '';
    if (hostProtocol !== PROTOCOL_VERSION) {
      console.warn(
        `[proxy] protocol mismatch: host speaks ${hostProtocol}, extension speaks ${PROTOCOL_VERSION}. Reload the extension.`
      );
    }
    broadcastBridgeState(true);
    return;
  }

  if (message.type === 'api_request' || message.type === 'api_request_chunk') {
    handleApiRequest(message).catch((error) => {
      console.error('[proxy] request failed:', error);
      respondError(message.id, 'Extension could not handle the request: ' + error.message);
    });
    return;
  }

  if (message.type === 'status_request') {
    // Asked by the host when /diagnostics is hit. This arrives on the native
    // port, not through runtime messaging, so it has to be handled here — it
    // used to be answered only in the runtime listener, which the host can
    // never reach, leaving /diagnostics with an empty "extension" field.
    getCanvasState()
      .then((state) => {
        postToHost({
          type: 'ext_status',
          id: message.id,
          data: {
            nativeConnected: hostReady,
            protocol: hostProtocol,
            port: hostPort,
            hostError,
            canvasTabId: state.tabId,
            canvasReady: Boolean(state.ready),
            lastResponse
          }
        });
      })
      .catch((error) => {
        postToHost({ type: 'ext_status', id: message.id, data: { error: error.message } });
      });
  }
}

function postToHost(message) {
  if (!nativePort) return false;
  try {
    nativePort.postMessage(message);
    return true;
  } catch (error) {
    console.error('[proxy] postMessage to host failed:', error);
    return false;
  }
}

function respondError(id, error) {
  postToHost({ type: 'api_response', id, error });
}

/**
 * Tell every Gemini tab whether the host is genuinely up. Tabs that have not
 * handshaked yet ignore this; they ask for state themselves when they do.
 */
function broadcastBridgeState(connected) {
  chrome.tabs.query({ url: 'https://gemini.google.com/*' }, (tabs) => {
    for (const tab of tabs) {
      chrome.tabs.sendMessage(tab.id, { type: 'bridge_state', connected })
        .catch(() => { /* tab has no content script; nothing to tell */ });
    }
  });
}

// ── Chunk reassembly ────────────────────────────────────────────────────────

function decodeChunks(chunks) {
  const parts = chunks.map((chunk) => {
    const binary = atob(chunk);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  });

  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const combined = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    combined.set(part, offset);
    offset += part.length;
  }
  return { json: new TextDecoder('utf-8', { fatal: true }).decode(combined), byteLength: total };
}

async function storeChunk(message) {
  const { total_chunks: total, chunk_index: index, chunk_encoding: encoding } = message;
  if (!Number.isInteger(total) || total < 1 || total > 128) throw new Error('invalid total_chunks');
  if (!Number.isInteger(index) || index < 0 || index >= total) throw new Error('invalid chunk_index');
  if (encoding !== 'base64') throw new Error('unsupported chunk encoding');

  const key = CHUNK_KEY_PREFIX + message.id;
  const stored = await chrome.storage.session.get(key);
  const buffer = stored[key] || { chunks: new Array(total), total, updatedAt: Date.now() };

  if (buffer.total !== total) {
    await chrome.storage.session.remove(key);
    throw new Error('chunk count changed mid-transfer');
  }

  buffer.chunks[index] = message.chunk_data;
  buffer.updatedAt = Date.now();
  await chrome.storage.session.set({ [key]: buffer });
  return { key, buffer };
}

/**
 * Reassemble a chunked request. Returns the original request object, or null
 * when more chunks are still outstanding.
 */
async function reassembleIfComplete(message) {
  const { key, buffer } = await storeChunk(message);
  const received = buffer.chunks.filter((chunk) => chunk !== undefined).length;
  if (received < buffer.total) return null;

  await chrome.storage.session.remove(key);
  try {
    return JSON.parse(decodeChunks(buffer.chunks).json);
  } catch (error) {
    throw new Error('reassembled payload is not valid UTF-8 JSON: ' + error.message);
  }
}

async function sweepExpiredChunks() {
  const stored = await chrome.storage.session.get(null);
  const now = Date.now();
  const expired = Object.entries(stored).filter(([key, value]) => (
    key.startsWith(CHUNK_KEY_PREFIX) && value && now - value.updatedAt >= CHUNK_TTL_MS
  ));
  if (!expired.length) return;

  await chrome.storage.session.remove(expired.map(([key]) => key));
  for (const [key] of expired) {
    respondError(key.slice(CHUNK_KEY_PREFIX.length), 'incomplete chunk transfer expired');
  }
}

// ── Request routing ─────────────────────────────────────────────────────────

async function discoverCanvasTab() {
  const state = await getCanvasState();

  if (state.tabId) {
    try {
      const tab = await chrome.tabs.get(state.tabId);
      if (isGeminiUrl(tab.url)) return state.tabId;
    } catch {
      // Tab is gone; fall through to a fresh lookup.
    }
  }

  const tabs = await chrome.tabs.query({ url: 'https://gemini.google.com/*' });
  if (!tabs.length) {
    await setCanvasState(null, false);
    return null;
  }

  await setCanvasState(tabs[0].id, false);
  return tabs[0].id;
}

async function handleApiRequest(message) {
  let request = message;

  if (message.type === 'api_request_chunk') {
    try {
      request = await reassembleIfComplete(message);
    } catch (error) {
      console.error('[proxy] chunk reassembly failed:', error);
      respondError(message.id, error.message);
      return;
    }
    if (request === null) return; // still waiting on more chunks
  }

  const tabId = await discoverCanvasTab();
  if (!tabId) {
    respondError(
      request.id,
      'No Gemini tab found. Open gemini.google.com and put the proxy HTML in Canvas Preview.'
    );
    return;
  }

  let reply;
  try {
    reply = await chrome.tabs.sendMessage(tabId, {
      type: 'api_request',
      id: request.id,
      method: request.method,
      path: request.path,
      body: request.body,
      headers: request.headers || {},
      stream: Boolean(request.stream),
      streamOptions: request.stream_options || {}
    });
  } catch (error) {
    // No content script in that tab at all — usually a page that has not
    // finished loading, or one open before the extension was installed.
    await setCanvasState(tabId, false);
    // "Receiving end does not exist" almost always means the tab predates the
    // extension: content scripts are injected at load, not retroactively.
    respondError(
      request.id,
      'That Gemini tab was opened before the extension loaded, so it has no relay. '
      + 'Reload the tab, then press Preview again. (' + error.message + ')'
    );
    return;
  }

  if (!reply || !reply.ok) {
    const reason = (reply && reply.reason) ||
      'Canvas page is not connected. Put the proxy HTML in Canvas Preview.';
    respondError(request.id, reason);
  }
}

/**
 * Answers too big for one message arrive as numbered pieces; put them back in
 * order. In-memory only: a transfer takes seconds, and a worker that dies
 * mid-transfer has lost the request anyway.
 */
const pendingResponses = new Map();

function collectResponseChunk(message) {
  const { id, total_chunks: total, chunk_index: index, chunk_data: data } = message;
  if (!Number.isInteger(total) || total < 1 || total > 512) return null;
  if (!Number.isInteger(index) || index < 0 || index >= total) return null;
  if (typeof data !== 'string') return null;

  const now = Date.now();
  for (const [key, value] of pendingResponses) {
    if (now - value.updatedAt > 120000) pendingResponses.delete(key);
  }

  const entry = pendingResponses.get(id) || { parts: new Array(total), total, updatedAt: now };
  entry.updatedAt = now;
  if (entry.total !== total) {
    pendingResponses.delete(id);
    return null;
  }
  entry.parts[index] = data;
  pendingResponses.set(id, entry);

  const received = entry.parts.filter((part) => typeof part === 'string').length;
  if (received < total) return null;

  pendingResponses.delete(id);
  try {
    return JSON.parse(entry.parts.join(''));
  } catch (error) {
    return { status: 0, data: null, error: 'reassembled response was not valid JSON' };
  }
}

// ── Content-script messages ─────────────────────────────────────────────────

function isTrustedGeminiSender(sender) {
  if (!sender.tab || !Number.isInteger(sender.tab.id) || !sender.url) return false;
  try {
    return new URL(sender.url).origin === 'https://gemini.google.com';
  } catch {
    return false;
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!isTrustedGeminiSender(sender)) {
    sendResponse({ ok: false, reason: 'untrusted sender' });
    return false;
  }

  switch (message.type) {
    case 'page_ready':
      setCanvasState(sender.tab.id, true)
        .then(() => {
          console.log('[proxy] canvas page ready in tab', sender.tab.id);
          sendResponse({
            ok: true, nativeConnected: hostReady, protocol: hostProtocol,
            port: hostPort, error: hostError
          });
        })
        .catch((error) => sendResponse({ ok: false, reason: error.message }));
      return true;

    case 'status_request':
      // Asked by the host through the native port when /diagnostics is hit.
      // Everything the routing layer knows, so "which tab, and is its preview
      // actually mounted" is answerable without a devtools console.
      getCanvasState()
        .then((state) => sendResponse({
          ok: true,
          nativeConnected: hostReady,
          protocol: hostProtocol,
          port: hostPort,
          hostError,
          canvasTabId: state.tabId,
          canvasReady: Boolean(state.ready),
          lastResponse,
          inflightChunkBuffers: 0
        }))
        .catch((error) => sendResponse({ ok: false, reason: error.message }));
      return true;

    case 'bridge_state_request':
      sendResponse({
        ok: true, nativeConnected: hostReady, protocol: hostProtocol,
        port: hostPort, error: hostError
      });
      return false;

    case 'api_response_chunk': {
      const assembled = collectResponseChunk(message);
      if (assembled === null) {
        sendResponse({ ok: true, pending: true });
        return false;
      }
      message = Object.assign({}, message, assembled);
      // fall through to the single-message path below
    }
    // eslint-disable-next-line no-fallthrough
    case 'api_response': {
      // Only the tab we route to may answer, otherwise a stale tab could
      // inject results into someone else's request.
      //
      // `activeTabId` is module state, and Chrome evicts an idle service worker
      // after ~30 s. A text answer lands well inside that window; an image job
      // runs for minutes, so by the time it finishes the worker has restarted
      // with `activeTabId` back at null and the real answer is rejected as
      // "inactive Gemini tab" — dropped silently, while the host waits out its
      // full timeout. Restore the tab from storage before judging the sender.
      getCanvasState()
        .then(() => {
          if (sender.tab.id !== activeTabId) {
            sendResponse({ ok: false, reason: 'inactive Gemini tab' });
            return;
          }
          let bytes = 0;
          try {
            bytes = JSON.stringify(message.data || null).length;
          } catch (error) {
            bytes = -1;
          }
          const ok = postToHost({
            type: 'api_response',
            id: message.id,
            status: message.status,
            data: message.data,
            error: message.error
          });
          lastResponse = { id: message.id, bytes, forwarded: ok, at: Date.now() };
          if (!ok) console.warn('[proxy] response could not be written to the native port');
          sendResponse({ ok });
        })
        .catch((error) => sendResponse({ ok: false, reason: error.message }));
      return true;
    }

    case 'api_stream_event': {
      const forward = () => {
        if (sender.tab.id !== activeTabId) {
          sendResponse({ ok: false, reason: 'inactive Gemini tab' });
          return;
        }
        // Forwarded synchronously: ordering between deltas is the whole point
        // of a stream, and an await here would let them interleave.
        sendResponse({ ok: postToHost({
          type: 'api_stream_event',
          id: message.id,
          event: message.event,
          text: message.text,
          tool_call: message.tool_call,
          finish_reason: message.finish_reason,
          usage: message.usage,
          error: message.error
        }) });
      };

      // Same eviction problem as api_response: a long stream can outlive the
      // worker. Only pay for storage when the id is actually missing, so the
      // steady state stays synchronous and deltas keep their order.
      if (activeTabId === null) {
        getCanvasState()
          .then(forward)
          .catch((error) => sendResponse({ ok: false, reason: error.message }));
        return true;
      }
      forward();
      return false;
    }

    default:
      sendResponse({ ok: false, reason: 'unknown message type' });
      return false;
  }
});

// ── Tab lifecycle ───────────────────────────────────────────────────────────

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (tabId !== activeTabId) return;
  // `loading` covers reloads and in-tab navigation. Either way the Canvas
  // page that was serving requests no longer exists.
  if (changeInfo.status === 'loading') {
    setCanvasState(tabId, false).catch(() => {});
  } else if (tab.url && !isGeminiUrl(tab.url)) {
    setCanvasState(null, false).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === activeTabId) {
    console.log('[proxy] canvas tab closed');
    setCanvasState(null, false).catch(() => {});
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === CHUNK_SWEEP_ALARM) {
    sweepExpiredChunks().catch((error) => console.error('[proxy] chunk sweep failed:', error));
  }
});

// ── Start ───────────────────────────────────────────────────────────────────

connectNative();
getCanvasState().catch(() => {});
chrome.alarms.create(CHUNK_SWEEP_ALARM, { periodInMinutes: 0.5 });
sweepExpiredChunks().catch(() => {});
