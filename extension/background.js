/**
 * background.js — Service Worker
 *
 * Routes messages between the native messaging host (Python HTTP server)
 * and the content script (which relays to the Canvas iframe).
 *
 * Architecture:
 *   Native Host (Python :8765)
 *       ↕ stdio (4-byte length + JSON)
 *   This service worker
 *       ↕ chrome.tabs.sendMessage
 *   Content script (in top-level Gemini page)
 *       ↕ postMessage
 *   Canvas iframe (proxy page with free Gemini API key)
 *
 * The native host connects via chrome.runtime.connectNative().
 * Chrome starts the Python process and keeps it alive while the
 * port is open. If the Python process dies, we reconnect after 2s.
 *
 * Tab discovery: We look for any tab with "gemini" or "canvas" in
 * the URL. We also listen for page_ready messages from the content
 * script, which fires when the Canvas proxy page loads.
 */

let nativePort = null;
let canvasTabId = null;

// ── Native messaging host connection ─────────────────────────────────────────

function connectNative() {
    if (nativePort) return;

    try {
        nativePort = chrome.runtime.connectNative('com.gemini.proxy');
        console.log('[Proxy] Connected to native host');
    } catch (e) {
        console.error('[Proxy] Failed to connect to native host:', e);
        setTimeout(connectNative, 2000);
        return;
    }

    // Messages from the Python HTTP server
    nativePort.onMessage.addListener((msg) => {
        if (msg.type === 'api_request') {
            handleApiRequest(msg);
        }
    });

    // Python process died — reconnect
    nativePort.onDisconnect.addListener(() => {
        console.warn('[Proxy] Native host disconnected, reconnecting...');
        nativePort = null;
        setTimeout(connectNative, 2000);
    });
}

// ── API request forwarding ───────────────────────────────────────────────────

async function handleApiRequest(msg) {
    // Discover the Canvas tab if we don't have one
    if (!canvasTabId) {
        await discoverCanvasTab();
    }

    if (!canvasTabId) {
        const err = 'No Canvas tab found. Open gemini.google.com, paste proxy HTML in Code view, click Preview.';
        console.error('[Proxy]', err);
        if (nativePort) {
            nativePort.postMessage({ type: 'api_response', id: msg.id, error: err });
        }
        return;
    }

    // Programmatically inject content script (in case it wasn't auto-injected)
    try {
        await chrome.scripting.executeScript({
            target: { tabId: canvasTabId, allFrames: true },
            files: ['content_script.js']
        });
    } catch (e) {
        // Already injected or sandbox restriction — that's OK
    }

    // If the payload was too large for native messaging (>1MB), fetch it via HTTP.
    // Extension service workers can fetch() from localhost without Local Network
    // Access restrictions — this bypasses the 1MB native messaging limit entirely.
    // We try 127.0.0.1 first, then localhost as a fallback (some environments
    // like Chromium snap may resolve one but not the other).
    let body = msg.body;
    if (msg.fetch_payload) {
        const urls = [msg.payload_url, msg.payload_url_alt].filter(Boolean);
        let fetched = false;
        for (const url of urls) {
            try {
                console.log('[Proxy] Fetching large payload from', url);
                const resp = await fetch(url);
                if (resp.ok) {
                    body = await resp.json();
                    console.log('[Proxy] Payload fetched, size:', JSON.stringify(body).length, 'bytes');
                    fetched = true;
                    break;
                }
            } catch (err) {
                console.warn('[Proxy] Fetch failed for', url, ':', err.message);
            }
        }
        if (!fetched) {
            console.error('[Proxy] All payload fetch attempts failed');
            if (nativePort) {
                nativePort.postMessage({
                    type: 'api_response',
                    id: msg.id,
                    error: 'Failed to fetch large payload from all URLs'
                });
            }
            return;
        }
    }

    // Forward the API request to the content script
    try {
        await chrome.tabs.sendMessage(canvasTabId, {
            type: 'api_request',
            id: msg.id,
            method: msg.method,
            path: msg.path,
            body: body,
            headers: msg.headers || {}
        });
    } catch (err) {
        console.warn('[Proxy] Failed to send to tab:', err.message);
        if (nativePort) {
            nativePort.postMessage({
                type: 'api_response',
                id: msg.id,
                error: 'Canvas tab not responding. Make sure proxy HTML is in Canvas Preview. Error: ' + err.message
            });
        }
    }
}

// ── Canvas tab discovery ─────────────────────────────────────────────────────

function discoverCanvasTab() {
    return new Promise((resolve) => {
        chrome.tabs.query({}, (tabs) => {
            for (const tab of tabs) {
                if (!tab.url) continue;
                const url = tab.url.toLowerCase();
                // Match various Gemini URLs (gemini.google.com/app, etc.)
                if (url.includes('gemini.google.com')) {
                    canvasTabId = tab.id;
                    console.log('[Proxy] Found Gemini tab:', canvasTabId, tab.url.substring(0, 60));
                    resolve(tab.id);
                    return;
                }
            }
            console.warn('[Proxy] No Gemini tab found among', tabs.length, 'tabs');
            canvasTabId = null;
            resolve(null);
        });
    });
}

// ── Message listeners (from content script) ──────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === 'page_ready') {
        canvasTabId = sender.tab.id;
        console.log('[Proxy] Canvas proxy page ready, tab:', canvasTabId);
        if (nativePort) {
            nativePort.postMessage({ type: 'page_ready', tabId: canvasTabId });
        }
        sendResponse({ ok: true });
    }

    if (message.type === 'api_response') {
        if (nativePort) {
            nativePort.postMessage({
                type: 'api_response',
                id: message.id,
                status: message.status,
                data: message.data,
                error: message.error
            });
        }
    }

    return true; // Keep message channel open for async responses
});

// ── Tab lifecycle tracking ───────────────────────────────────────────────────

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (tab.url) {
        const url = tab.url.toLowerCase();
        if (url.includes('gemini.google.com')) {
            canvasTabId = tabId;
        } else if (tabId === canvasTabId) {
            canvasTabId = null;
        }
    }
});

chrome.tabs.onRemoved.addListener((tabId) => {
    if (tabId === canvasTabId) {
        console.log('[Proxy] Canvas tab closed');
        canvasTabId = null;
    }
});

// ── Start ────────────────────────────────────────────────────────────────────

connectNative();
discoverCanvasTab();
