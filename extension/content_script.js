/**
 * content_script.js — PostMessage Relay
 *
 * This script runs in the top-level Gemini page (gemini.google.com).
 * It cannot run inside the sandboxed Canvas iframe, but it CAN
 * communicate with it via postMessage (browser-level IPC, not blocked).
 *
 * Communication flow:
 *   Canvas iframe ──postMessage──→ This content script
 *                ←─────────────────
 *                         │ chrome.runtime.sendMessage
 *                         ▼
 *                   background.js (service worker)
 *
 * Deduplication is handled on the Canvas proxy page side (by request ID),
 * NOT here — we post to all iframes because the Canvas iframe is sandboxed
 * and event.source matching is unreliable across sandbox boundaries.
 */

// ── Listen for messages from the Canvas iframe (via postMessage) ────────────

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

        // Send to ALL iframes — the Canvas preview iframe will pick it up.
        // We post to all because sandbox cross-origin restrictions make
        // iframe.contentWindow matching unreliable. Deduplication is handled
        // on the Canvas proxy page by tracking request IDs.
        document.querySelectorAll('iframe').forEach(iframe => {
            try { iframe.contentWindow.postMessage(payload, '*'); } catch (e) {}
        });

        // Also send to main window (in case proxy code is in top-level page)
        window.postMessage(payload, '*');
    }
});

// ── 3. Automatic Canvas Card Clicker ──────────────────────────────────────────

let targetChatId = '';
let canvasCardName = '';
let autoClickInterval = null;

function autoClickCanvasCard() {
    if (!targetChatId || !canvasCardName) return;

    // If the Canvas iframe is already present, the Canvas panel is already open.
    if (document.querySelector('iframe')) return;

    // 1. Check if the target chat link is visible in the DOM
    const chatLink = document.querySelector(`a[href*="${targetChatId}"]`);
    if (chatLink) {
        // If we are currently on the welcome page (no chat ID in path), click the link to load it
        if (!window.location.pathname.includes(targetChatId)) {
            console.log("[Canvas Proxy Extension] Found chat link in sidebar, clicking it:", chatLink);
            chatLink.click();
            return;
        }
    } else {
        // If the link is not found, the sidebar might be closed. Look for the "Open sidebar" toggle button.
        const sidebarToggle = document.querySelector('[aria-label*="sidebar" i], [aria-label*="menu" i], [title*="sidebar" i]');
        if (sidebarToggle) {
            const label = (sidebarToggle.getAttribute('aria-label') || '').toLowerCase();
            const title = (sidebarToggle.getAttribute('title') || '').toLowerCase();
            if (label.includes('open') || label.includes('expand') || title.includes('open') || title.includes('expand')) {
                console.log("[Canvas Proxy Extension] Sidebar is closed, clicking toggle to open:", sidebarToggle);
                sidebarToggle.click();
                return;
            }
        }
    }

    // 2. Look for the target card inside the page (when the chat thread is loaded)
    const elements = document.querySelectorAll('div, span, button, p');
    for (const el of elements) {
        if (el.textContent && el.textContent.trim().toLowerCase() === canvasCardName.toLowerCase()) {
            // Find the closest button or clickable wrapper, or default to the element
            const clickable = el.closest('button') || el.closest('[role="button"]') || el;
            console.log("[Canvas Proxy Extension] Auto-clicking Canvas card:", clickable);
            
            // Dispatch standard click and mousedown/mouseup sequence for React compatibility
            clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            clickable.click();
            break;
        }
    }
}

// Request configuration from background script
try {
    chrome.runtime.sendMessage({ type: 'get_config' }, (config) => {
        if (config && config.target_chat_id && config.canvas_card_name) {
            targetChatId = config.target_chat_id;
            canvasCardName = config.canvas_card_name;
            console.log("[Canvas Proxy Extension] Auto-clicker enabled for Chat ID:", targetChatId, "Card:", canvasCardName);
            // Check every 1.5 seconds for the Canvas card if not already open
            autoClickInterval = setInterval(autoClickCanvasCard, 1500);
        } else {
            console.log("[Canvas Proxy Extension] Auto-clicker disabled (no target chat/card configured).");
        }
    });
} catch (e) {
    console.error("[Canvas Proxy Extension] Failed to get config from background:", e);
}
