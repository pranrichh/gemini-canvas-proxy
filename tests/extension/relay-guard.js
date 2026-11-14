/**
 * Proves the service-worker relay guard without a browser.
 *
 * The bug: an answer from the Canvas tab is rejected unless sender.tab.id
 * matches `activeTabId`, which is module state. Simulate the state Chrome
 * leaves behind after evicting an idle worker — module variable null, storage
 * still holding the tab — and check that the answer is forwarded.
 *
 * Run: node tests/extension/relay-guard.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SOURCE = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, '..', '..', 'extension', 'background.js');

function makePort() {
  return {
    posted: [],
    onMessage: { addListener() {} },
    onDisconnect: { addListener() {} },
    postMessage(message) { this.posted.push(message); },
    disconnect() {},
  };
}

function loadBackground(stateInStorage, storedTabId) {
  const port = makePort();
  const listeners = [];
  const store = {};
  if (stateInStorage) store.canvasState = { tabId: storedTabId, ready: true };

  const chrome = {
    runtime: {
      onMessage: { addListener: (fn) => listeners.push(fn) },
      connectNative: () => port,
      lastError: undefined,
      sendMessage: async () => ({ ok: true }),
    },
    storage: {
      session: {
        get: async (key) => (key === null ? { ...store } : { [key]: store[key] }),
        set: async (obj) => { Object.assign(store, obj); },
        remove: async (key) => {
          for (const k of [].concat(key)) delete store[k];
        },
      },
    },
    tabs: {
      query: (_query, callback) => callback([]),
      get: async (id) => ({ id, url: 'https://gemini.google.com/app' }),
      sendMessage: async () => ({ ok: true }),
      onRemoved: { addListener() {} },
      onUpdated: { addListener() {} },
    },
    alarms: {
      create() {},
      onAlarm: { addListener() {} },
    },
  };

  const context = vm.createContext({
    chrome, console: { log() {}, warn() {}, error() {} }, setTimeout, clearTimeout,
    URL, atob, btoa, TextDecoder, TextEncoder, Uint8Array, Uint32Array,
  });
  vm.runInContext(fs.readFileSync(SOURCE, 'utf8'), context, { filename: 'background.js' });
  return { handler: listeners[0], port };
}

function deliver(handler, port, tabId) {
  const reply = { ok: null };
  const replied = new Promise((resolve) => {
    handler(
      { type: 'api_response', id: 'req-1', status: 200, data: { ok: true }, error: null },
      { tab: { id: tabId }, url: 'https://gemini.google.com/app' },
      (response) => { reply.ok = response && response.ok; resolve(); },
    );
  });
  return replied.then(() => reply);
}

function sendChunk(handler, id, parts, index) {
  return new Promise((resolve) => {
    handler(
      { type: 'api_response_chunk', id, total_chunks: parts.length,
        chunk_index: index, chunk_data: parts[index] },
      { tab: { id: 5 }, url: 'https://gemini.google.com/app' },
      () => resolve(),
    );
  });
}

(async () => {
  // After a worker eviction: the module variable is null, storage still knows
  // which tab owns the request.
  const cold = loadBackground(true, 5);
  assert.strictEqual(cold.port.posted.length, 0);
  const coldReply = await deliver(cold.handler, cold.port, 5);
  const forwarded = cold.port.posted.filter((m) => m.type === 'api_response');

  assert.ok(forwarded.length === 1,
    'a cold worker dropped the answer instead of restoring the tab');
  assert.strictEqual(forwarded[0].id, 'req-1');
  assert.strictEqual(coldReply.ok, true);

  // A tab that was never the Canvas tab still must not inject a result.
  const stranger = loadBackground(true, 5);
  const strangerReply = await deliver(stranger.handler, stranger.port, 9);
  assert.strictEqual(stranger.port.posted.filter((m) => m.type === 'api_response').length, 0,
    'a foreign tab was allowed to answer');
  assert.strictEqual(strangerReply.ok, false);

  // A response split into pieces, arriving out of order, reassembles once.
  const body = { status: 200, data: { image: 'x'.repeat(500) }, error: null };
  const json = JSON.stringify(body);
  const size = 120;
  const parts = [];
  for (let i = 0; i < json.length; i += size) parts.push(json.slice(i, i + size));

  const chunked = loadBackground(true, 5);
  // Every piece, newest-first, so ordering is exercised rather than assumed.
  const order = parts.map((_, index) => index).sort((a, b) => b - a);
  for (const index of order.slice(0, -1)) {
    await sendChunk(chunked.handler, 'req-3', parts, index);
    assert.strictEqual(chunked.port.posted.filter((m) => m.type === 'api_response').length, 0,
      'a partial set of chunks was forwarded');
  }
  const lastIndex = order[order.length - 1];
  await sendChunk(chunked.handler, 'req-3', parts, lastIndex);

  const rebuilt = chunked.port.posted.filter((m) => m.type === 'api_response');
  assert.strictEqual(rebuilt.length, 1, 'the last chunk did not complete the answer');
  assert.strictEqual(rebuilt[0].data.image.length, 500, 'the reassembled payload lost bytes');

  console.log('relay guard: cold worker forwards, foreign tab refused, chunks reassemble');
})();
