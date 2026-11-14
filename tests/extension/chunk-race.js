/**
 * Proves two chunks arriving in the same tick do not erase each other.
 *
 * Chunk assembly reads a buffer out of chrome.storage.session, adds one index,
 * and writes it back. Two chunks arriving together both read the same empty
 * buffer, each writes only its own index, and the second write wins — one chunk
 * is lost, the set never completes, and ninety seconds later the transfer dies
 * with "incomplete chunk transfer expired". Intermittent, and only for requests
 * big enough to need more than one chunk.
 *
 * Run: node tests/extension/chunk-race.js [path/to/background.js]
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SOURCE = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, '..', '..', 'extension', 'background.js');

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function loadBackground() {
  const store = {};
  const delivered = [];
  const responses = [];
  const port = {
    posted: [],
    onMessage: { addListener() {} },
    onDisconnect: { addListener() {} },
    postMessage(message) { this.posted.push(message); if (message.type === 'api_response') responses.push(message); },
    disconnect() {},
  };
  const listeners = [];

  const chrome = {
    runtime: {
      onMessage: { addListener: (fn) => listeners.push(fn) },
      connectNative: () => port,
      lastError: undefined,
      sendMessage: async () => ({ ok: true }),
    },
    storage: {
      session: {
        // The delay is the point: it lets a second chunk read before the first
        // one has written.
        get: async (key) => {
          await wait(12);
          if (key === null) return JSON.parse(JSON.stringify(store));
          return key in store ? { [key]: JSON.parse(JSON.stringify(store[key])) } : {};
        },
        set: async (obj) => { await wait(12); Object.assign(store, JSON.parse(JSON.stringify(obj))); },
        remove: async (key) => { await wait(4); for (const k of [].concat(key)) delete store[k]; },
      },
    },
    tabs: {
      query: (_query, callback) => {
        const tabs = [{ id: 5, url: 'https://gemini.google.com/app' }];
        if (callback) { callback(tabs); return undefined; }
        return Promise.resolve(tabs);
      },
      get: async (id) => ({ id, url: 'https://gemini.google.com/app' }),
      sendMessage: async (tabId, message) => { delivered.push(message); return { ok: true }; },
      onRemoved: { addListener() {} },
      onUpdated: { addListener() {} },
    },
    alarms: { create() {}, onAlarm: { addListener() {} } },
  };

  const context = vm.createContext({
    chrome, console: { log() {}, warn() {}, error() {} }, setTimeout, clearTimeout,
    URL, atob, btoa, TextDecoder, TextEncoder, Uint8Array, Uint32Array, JSON, Promise,
  });
  vm.runInContext(fs.readFileSync(SOURCE, 'utf8'), context, { filename: 'background.js' });
  return { context, delivered, responses, port };
}

function chunkOf(id, index, total, text) {
  return {
    type: 'api_request_chunk', id,
    total_chunks: total, chunk_index: index, chunk_encoding: 'base64',
    chunk_data: Buffer.from(text, 'utf8').toString('base64'),
  };
}

(async () => {
  const bg = loadBackground();

  const body = { model: 'gemini-3-flash-preview', messages: [{ role: 'user', content: 'x'.repeat(200) }] };
  const serialized = JSON.stringify({
    id: 'race-1', method: 'POST', path: '/v1beta/models/gemini-3-flash-preview:generateContent',
    body, headers: {}, stream: false,
  });

  const half = Math.floor(serialized.length / 2);
  const a = chunkOf('race-1', 0, 2, serialized.slice(0, half));
  const b = chunkOf('race-1', 1, 2, serialized.slice(half));

  // Both in the same tick, which is what the native port does when the host
  // writes two frames back to back.
  await Promise.all([
    bg.context.onNativeMessage(a),
    bg.context.onNativeMessage(b),
  ]);
  await wait(120);

  assert.strictEqual(bg.delivered.length, 1,
    'the request was not delivered exactly once (' + bg.delivered.length + ')');
  assert.deepStrictEqual(bg.delivered[0].body, body,
    'the reassembled request body is wrong — a chunk was lost');

  const expired = bg.responses.filter((r) => /incomplete chunk/i.test(r.error || ''));
  assert.strictEqual(expired.length, 0, 'a transfer expired instead of completing');

  console.log('chunk assembly: concurrent chunks both survive, request delivered once');
})();
