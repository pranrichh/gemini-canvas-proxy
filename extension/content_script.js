/**
 * Content-script relay.
 *
 * The Canvas preview runs in a sandboxed iframe, which is why it cannot open a
 * WebSocket to localhost (Chrome's Local Network Access blocks that). It can,
 * however, hand one end of a MessageChannel to its parent during a postMessage
 * handshake. This script receives that port and shuttles messages between it
 * and the service worker.
 *
 * It also answers liveness pings. That matters: the ack we send on handshake
 * only proves *this* script exists, not that the native host is up. The pong
 * carries the service worker's view of the host so the page can tell the
 * difference.
 */

/** The MessageChannel port handed over by the Canvas page. */
let proxyPort = null;

/** Last known native-host state, mirrored from the service worker. */
let bridgeState = { nativeConnected: false, protocol: 0, port: 0, error: '' };

/** Pings between background refreshes. 12 × 5 s keeps the worker quiet. */
const REFRESH_EVERY_N_PINGS = 12;
let pingCount = 0;

/**
 * Resolve whether `source` is the Canvas preview frame.
 *
 * Two rules, because Gemini has shipped both layouts:
 *   1. a direct child iframe of this document, or
 *   2. any frame with an opaque origin — a sandboxed iframe reports its origin
 *      as the string "null", and the Canvas preview is the only such frame on
 *      the page worth trusting.
 */
function isCanvasFrame(source, origin) {
  const direct = Array.from(document.querySelectorAll('iframe'))
    .some((iframe) => iframe.contentWindow === source);
  if (direct) return 'direct';

  // Canvas serves the preview from a blob: URL on Google's usercontent domain,
  // sometimes nested rather than a direct child of this document. A cross-origin
  // wrapper cannot be walked, so the origin is what identifies it. This is the
  // permissive rule the working v1 content script used; the port is the only
  // thing a frame can obtain here, and the page still has to speak the protocol.
  if (source && typeof origin === 'string') {
    if (origin === 'null') return 'sandboxed';
    if (/\.usercontent\.goog$/.test(origin) || origin === 'https://gemini.google.com') {
      return 'usercontent';
    }
  }
  return null;
}

function refreshBridgeState() {
  chrome.runtime.sendMessage({ type: 'bridge_state_request' })
    .then((state) => {
      if (state && state.ok) {
        bridgeState = {
          nativeConnected: Boolean(state.nativeConnected),
          protocol: state.protocol || 0,
          port: state.port || 0,
          error: state.error || ''
        };
      }
    })
    .catch(() => { /* service worker asleep; the next ping retries */ });
}

/**
 * Hand one answer to the service worker.
 *
 * An image or a speech clip is megabytes of base64, and a message that size
 * does not survive the hop, so anything large goes as numbered pieces the
 * worker reassembles.
 *
 * The pieces are sent one after another, each awaited, with a retry. Firing
 * them in a tight loop looked equivalent and was not: the first message wakes a
 * sleeping service worker and the rest can be refused while it is still
 * starting, which loses a chunk, leaves the set incomplete, and the host waits
 * out its timeout on a request the page finished long ago.
 */
const RESPONSE_CHUNK_CHARS = 192 * 1024;
const SEND_ATTEMPTS = 4;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function sendToWorker(message) {
  for (let attempt = 0; attempt < SEND_ATTEMPTS; attempt += 1) {
    try {
      await chrome.runtime.sendMessage(message);
      return true;
    } catch (error) {
      // "Could not establish connection" while the worker boots, or a torn
      // down context. Both are worth one more try before the answer is lost.
      await sleep(120 * (attempt + 1));
    }
  }
  return false;
}

async function deliverResponse(message) {
  let body;
  try {
    body = { status: message.status, data: message.data === undefined ? null : message.data,
             error: message.error === undefined ? null : message.error };
  } catch (error) {
    body = { status: 0, data: null, error: 'response could not be read: ' + error.message };
  }

  let json;
  try {
    json = JSON.stringify(body);
  } catch (error) {
    json = JSON.stringify({ status: 0, data: null,
                            error: 'response was not serialisable: ' + error.message });
  }

  if (json.length <= RESPONSE_CHUNK_CHARS) {
    await sendToWorker(Object.assign({ type: 'api_response', id: message.id }, JSON.parse(json)));
    return;
  }

  const total = Math.ceil(json.length / RESPONSE_CHUNK_CHARS);
  for (let index = 0; index < total; index += 1) {
    const ok = await sendToWorker({
      type: 'api_response_chunk',
      id: message.id,
      total_chunks: total,
      chunk_index: index,
      chunk_data: json.slice(index * RESPONSE_CHUNK_CHARS, (index + 1) * RESPONSE_CHUNK_CHARS)
    });
    if (!ok) return;
  }
}

window.addEventListener('message', (event) => {
  const data = event.data;
  if (!data || data.source !== 'gemini-proxy-ready') return;

  const frameKind = isCanvasFrame(event.source, event.origin);
  if (!frameKind || event.ports.length !== 1) return;

  if (proxyPort) proxyPort.close();
  proxyPort = event.ports[0];

  proxyPort.onmessage = (portEvent) => {
    const message = portEvent.data;
    if (!message) return;

    if (message.source === 'gemini-proxy-response') {
      deliverResponse(message).catch((error) => {
        console.error('[proxy] could not hand the answer over:', error);
      });
      return;
    }

    if (message.source === 'gemini-proxy-stream') {
      chrome.runtime.sendMessage({
        type: 'api_stream_event',
        id: message.id,
        event: message.event,
        text: message.text,
        tool_call: message.tool_call,
        finish_reason: message.finish_reason,
        usage: message.usage,
        error: message.error
      }).catch(() => {});
      return;
    }

    if (message.source === 'gemini-proxy-ping') {
      // Answer immediately from the cached value, then refresh in the
      // background so the next pong is current.
      proxyPort.postMessage({
        source: 'gemini-proxy-pong',
        seq: message.seq,
        nativeConnected: bridgeState.nativeConnected,
        protocol: bridgeState.protocol,
        port: bridgeState.port,
        error: bridgeState.error
      });
      pingCount += 1;
      if (pingCount % REFRESH_EVERY_N_PINGS === 0) refreshBridgeState();
    }
  };

  proxyPort.start();

  chrome.runtime.sendMessage({ type: 'page_ready' })
    .then((reply) => {
      if (reply && reply.ok) {
        bridgeState = {
          nativeConnected: Boolean(reply.nativeConnected),
          protocol: reply.protocol || 0
        };
      }
      proxyPort.postMessage({
        source: 'gemini-proxy-ack',
        nativeConnected: bridgeState.nativeConnected,
        protocol: bridgeState.protocol,
        port: bridgeState.port,
        error: bridgeState.error
      });
    })
    .catch(() => {
      // The worker refused the page (wrong tab, extension reloading). Say so
      // rather than leaving the page waiting on a handshake that never lands.
      proxyPort.postMessage({
        source: 'gemini-proxy-ack', nativeConnected: false, protocol: 0, port: 0
      });
    });

  console.log('[proxy] canvas frame connected (' + frameKind + ')');
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!proxyPort) {
    // This script runs in every frame, and chrome.tabs.sendMessage delivers to
    // all of them — first answer wins. A sub-frame that has no port must stay
    // silent, otherwise it beats the top frame that does and every request is
    // reported as disconnected while the relay is fine.
    if (message.type === 'api_request' && window === window.top) {
      sendResponse({ ok: false, reason: 'Canvas page is not connected yet. Reload the preview.' });
    }
    return false;
  }

  if (message.type === 'bridge_state') {
    bridgeState.nativeConnected = Boolean(message.connected);
    proxyPort.postMessage({
      source: 'gemini-proxy-state',
      connected: Boolean(message.connected)
    });
    return false;
  }

  if (message.type !== 'api_request') return false;

  proxyPort.postMessage({
    source: 'gemini-proxy-request',
    id: message.id,
    method: message.method,
    path: message.path,
    body: message.body,
    headers: message.headers,
    stream: Boolean(message.stream),
    streamOptions: message.streamOptions || {}
  });

  // The reply is the delivery receipt. Without it the service worker cannot
  // distinguish "the canvas is working on it" from "nobody is listening",
  // and would wait out the full timeout on a request that never left the tab.
  sendResponse({ ok: true });
  return false;
});
