/**
 * content_script.js — PostMessage Relay
 *
 * This script runs in the TOP-LEVEL Gemini page (canvas.gemini.google.com
 * or gemini.google.com/app). It cannot run directly inside the sandboxed
 * Canvas preview iframe, but it CAN communicate with it via postMessage
 * — which works across sandbox boundaries because it's a browser-level
 * mechanism, not a network call.
 *
 * Communication flow:
 *   ┌─────────────────┐     postMessage      ┌──────────────────────┐
 *   │ Canvas iframe   │ ───────────────────→  │ This content script  │
 *   │ (proxy page)    │ ←───────────────────  │ (top-level page)     │
 *   └─────────────────┘                      └──────────┬───────────┘
 *                                                        │ chrome.runtime
 *                                                        │ .sendMessage
 *                                                        ▼
 *                                            ┌──────────────────────┐
 *                                            │ background.js        │
 *                                            │ (service worker)     │
 *                                            └──────────────────────┘
 *
 * Three message types flow through here:
 *   1. page_ready        — iframe announces it loaded (page → bg)
 *   2. api_request       — background forwards API call (bg → iframe)
 *   3. api_response      — iframe returns API result (iframe → bg)
 */

// ── Listen for messages from the Canvas iframe (via postMessage) ─────────────

window.addEventListener('message', (event) => {
    const data = event.data;
    if (!data) return;

    // Canvas proxy page is ready — notify background
    if (data.source === 'gemini-proxy-ready') {
        chrome.runtime.sendMessage({ type: 'page_ready' });
        return;
    }

    // API response from the Canvas proxy page — forward to background
    if (data.source === 'gemini-proxy-response') {
        chrome.runtime.sendMessage({
            type: 'api_response',
            id: data.id,
            status: data.status,
            data: data.data,
            error: data.error
        });
        return;
    }
});

// ── Listen for messages from background (API requests to forward to iframe) ──

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === 'api_request') {
        const payload = {
            source: 'gemini-proxy-request',
            id: message.id,
            method: message.method,
            path: message.path,
            body: message.body,
            headers: message.headers
        };

        // Send to ALL iframes — the Canvas preview iframe will pick it up
        document.querySelectorAll('iframe').forEach(iframe => {
            try { iframe.contentWindow.postMessage(payload, '*'); } catch (e) {}
        });

        // Also send to main window (in case proxy code is in top-level page)
        window.postMessage(payload, '*');
    }
});
