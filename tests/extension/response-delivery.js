/**
 * Proves the page's answer reaches the worker in pieces, reliably.
 *
 * The failure this guards against: chunks fired in a tight loop at a sleeping
 * service worker. The first message wakes it, the rest are refused while it
 * boots, `.catch` throws the failures away, one chunk is missing and the set
 * never completes — the page shows a finished job and the host waits out its
 * timeout. Intermittent, which is the worst kind.
 *
 * Run: node tests/extension/response-delivery.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SOURCE = path.join(__dirname, '..', '..', 'extension', 'content_script.js');

function loadDelivery({ failWhen = () => false } = {}) {
  const sent = [];
  let attempts = 0;

  const chrome = {
    runtime: {
      sendMessage: async (message) => {
        attempts += 1;
        if (failWhen(message, attempts)) throw new Error('Could not establish connection');
        sent.push(message);
        return { ok: true };
      },
      onMessage: { addListener() {} },
    },
  };

  const context = vm.createContext({
    chrome,
    window: { addEventListener() {}, top: {}, parent: {} },
    console: { log() {}, warn() {}, error() {} },
    setTimeout, clearTimeout, Promise, JSON, Math, Date, URL,
    atob, btoa, TextDecoder, TextEncoder, Uint8Array,
  });
  vm.runInContext(fs.readFileSync(SOURCE, 'utf8'), context, { filename: 'content_script.js' });
  return { deliver: context.deliverResponse, sent, attempts: () => attempts };
}

const CHUNK = 192 * 1024;

(async () => {
  // 1. A small answer goes as one message, not a pointless chunk set.
  const small = loadDelivery();
  await small.deliver({ id: 'small-1', status: 200, data: { text: 'ok' }, error: null });
  assert.strictEqual(small.sent.length, 1, 'a small answer was chunked');
  assert.strictEqual(small.sent[0].type, 'api_response');
  assert.strictEqual(small.sent[0].id, 'small-1');

  // 2. A large answer is split, in order, and reassembles to the original.
  const big = loadDelivery();
  const payload = 'A'.repeat(CHUNK * 2 + 5000);
  await big.deliver({ id: 'big-1', status: 200, data: { image: payload }, error: null });

  const chunks = big.sent.filter((m) => m.type === 'api_response_chunk');
  const expected = Math.ceil(JSON.stringify({ status: 200, data: { image: payload }, error: null }).length / CHUNK);
  assert.strictEqual(chunks.length, expected, 'wrong number of chunks');
  assert.deepStrictEqual(chunks.map((c) => c.chunk_index), [...Array(expected).keys()],
    'chunks were not sent in order, one after another');

  const rebuilt = JSON.parse(chunks.map((c) => c.chunk_data).join(''));
  assert.strictEqual(rebuilt.data.image.length, payload.length, 'reassembly lost bytes');

  // 3. The failure that caused the bug: the worker refuses the first attempts
  //    while it boots. Every piece must still arrive.
  let refused = 0;
  const retrying = loadDelivery({
    failWhen: (message, attempt) => attempt <= 3 && refused++ < 3,
  });
  await retrying.deliver({ id: 'big-2', status: 200, data: { image: payload }, error: null });
  const retriedChunks = retrying.sent.filter((m) => m.type === 'api_response_chunk');
  assert.strictEqual(retriedChunks.length, expected,
    'a transient refusal lost a chunk instead of being retried');

  // 4. A refusal that never resolves must not claim success.
  const dead = loadDelivery({ failWhen: () => true });
  await dead.deliver({ id: 'big-3', status: 200, data: { image: payload }, error: null });
  assert.strictEqual(dead.sent.length, 0, 'a failed delivery reported messages as sent');

  console.log('response delivery: small inline, large chunked in order, refusals retried');
})();
