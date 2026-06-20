#!/usr/bin/env python3
"""
Gemini Canvas Proxy — Native Messaging Host
============================================

A local HTTP server that exposes an OpenAI-compatible API endpoint,
backed by free unlimited Gemini inference via Gemini Canvas.

How it works:
    1. HTTP server listens on localhost:8765
    2. Incoming OpenAI-format requests are translated to Gemini format
    3. Translated requests are sent to the Chrome extension via stdio
       (Chrome native messaging protocol: 4-byte length + JSON)
    4. The extension forwards them to the Canvas page via postMessage
    5. The Canvas page calls the Gemini API with its auto-injected key
    6. Responses flow back: Canvas → extension → native host → HTTP

The Canvas internal API key is:
    - Unlimited (no rate limit, no daily cap)
    - Model-scoped (only works with the currently promoted model)
    - Session-bound (dies when the Canvas tab closes)
    - Auto-injected by Canvas when code contains `apiKey = ""`

Limitations:
    - The Canvas key rejects native function/functionResponse roles in
      conversation history. We work around this by converting tool calls
      and results to plain text messages (model still understands them).
    - 1MB max response size (Chrome native messaging limit)
    - Streaming is faked (single chunk + [DONE])

Credits:
    The postMessage bridge concept was inspired by coxcelot's "I am canceled"
    autobrowsing agent harness:
    https://github.com/coxcelot/iamcanceledpresentsagenericautobrowsingagentharness
"""

import struct
import sys
import json
import threading
import uuid
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue, Empty


# ═══════════════════════════════════════════════════════════════════════════
# NATIVE MESSAGING PROTOCOL
# ═══════════════════════════════════════════════════════════════════════════
# Chrome communicates with native hosts via stdin/stdout. Each message is:
#   [4-byte length (native endian)] [UTF-8 JSON payload]
# Limits: host→extension = 1MB, extension→host = 64MB

def read_message():
    """Read a single framed JSON message from Chrome's stdin."""
    raw = sys.stdin.buffer.read(4)
    if not raw or len(raw) < 4:
        return None
    length = struct.unpack('=I', raw)[0]
    if length == 0:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode('utf-8'))


def send_message(msg):
    """Send a framed JSON message to Chrome's stdout."""
    encoded = json.dumps(msg, separators=(',', ':')).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('=I', len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


# ═══════════════════════════════════════════════════════════════════════════
# REQUEST TRACKING
# ═══════════════════════════════════════════════════════════════════════════

pending_requests = {}  # request_id → Queue (for matching responses to requests)


# ═══════════════════════════════════════════════════════════════════════════
# FORMAT TRANSLATION: OpenAI Chat Completions → Gemini generateContent
# ═══════════════════════════════════════════════════════════════════════════

def openai_to_gemini(body):
    """
    Convert an OpenAI chat completions request to Gemini generateContent format.

    Key conversions:
        - messages[] → contents[] with role mapping (user→user, assistant→model)
        - system message → systemInstruction
        - temperature, max_tokens → generationConfig
        - tools[] → single tools[{functionDeclarations: [...]}] with UPPERCASE types
        - tool_calls in assistant history → text description (Canvas key rejects functionCall parts)
        - tool results → user message with [Tool result] prefix (Canvas key rejects function role)
    """
    contents = []
    system_instruction = None

    for msg in body.get('messages', []):
        role = msg.get('role', 'user')
        content = msg.get('content', '')

        # ── Assistant messages with tool_calls ────────────────────────────
        # Canvas key rejects functionCall parts in history, so we describe
        # the tool call as plain text. The model understands this context.
        if role == 'assistant' and msg.get('tool_calls'):
            call_descriptions = []
            for tc in msg.get('tool_calls', []):
                func = tc.get('function', {})
                call_descriptions.append(f"[Calling tool: {func.get('name', '')}({func.get('arguments', '{}')})]")
            text = (content + "\n" if content else "") + "\n".join(call_descriptions)
            contents.append({"role": "model", "parts": [{"text": text.strip()}]})
            continue

        # ── Tool results ──────────────────────────────────────────────────
        # Canvas key rejects the "function" role, so we send tool results
        # as user messages with a clear prefix. The model handles this fine.
        if role == 'tool':
            tool_call_id = msg.get('tool_call_id', '')
            func_name = ""
            for prev_msg in body.get('messages', []):
                if prev_msg.get('role') == 'assistant' and prev_msg.get('tool_calls'):
                    for tc in prev_msg['tool_calls']:
                        if tc.get('id') == tool_call_id:
                            func_name = tc.get('function', {}).get('name', '')
                            break
            result_text = f"[Tool result from {func_name}]: {content}"
            contents.append({"role": "user", "parts": [{"text": result_text}]})
            continue

        # ── Multimodal content (images) ───────────────────────────────────
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get('type') == 'text':
                    parts.append({"text": part['text']})
                elif part.get('type') == 'image_url':
                    url = part.get('image_url', {}).get('url', '')
                    if url.startswith('data:'):
                        meta, b64 = url.split(',', 1)
                        mime = meta.split(';')[0].split(':')[1] if ':' in meta else 'image/jpeg'
                        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
            if parts:
                if role == 'system':
                    system_instruction = {"parts": parts}
                elif role == 'assistant':
                    contents.append({"role": "model", "parts": parts})
                else:
                    contents.append({"role": "user", "parts": parts})
            continue

        # ── Plain text messages ───────────────────────────────────────────
        if role == 'system':
            system_instruction = {"parts": [{"text": content}]}
        elif role == 'assistant':
            contents.append({"role": "model", "parts": [{"text": content}]})
        else:
            contents.append({"role": "user", "parts": [{"text": content}]})

    result = {
        "contents": contents,
        "generationConfig": {}
    }

    if system_instruction:
        result["systemInstruction"] = system_instruction

    # ── Generation parameters ─────────────────────────────────────────────
    gc = result["generationConfig"]
    if 'temperature' in body:
        gc["temperature"] = body["temperature"]
    if 'max_tokens' in body:
        gc["maxOutputTokens"] = body["max_tokens"]
    if 'top_p' in body:
        gc["topP"] = body["top_p"]
    if 'max_completion_tokens' in body:
        gc["maxOutputTokens"] = body["max_completion_tokens"]

    # ── Tool definitions ──────────────────────────────────────────────────
    # Canvas requires: single tools object, all functions in one array,
    # UPPERCASE type values (OBJECT, STRING, INTEGER, etc.)
    tools = body.get('tools', [])
    if tools:
        func_decls = []
        for tool in tools:
            if tool.get('type') == 'function':
                func = tool.get('function', {})
                params = func.get('parameters', {"type": "object", "properties": {}})
                params = _uppercase_types(params)
                func_decls.append({
                    "name": func.get('name', ''),
                    "description": func.get('description', ''),
                    "parameters": params
                })
        if func_decls:
            result["tools"] = [{"functionDeclarations": func_decls}]

    return result


def _uppercase_types(obj):
    """Recursively uppercase all 'type' field values in a JSON schema."""
    if isinstance(obj, dict):
        if 'type' in obj and isinstance(obj['type'], str):
            obj['type'] = obj['type'].upper()
        for v in obj.values():
            _uppercase_types(v)
    elif isinstance(obj, list):
        for item in obj:
            _uppercase_types(item)
    return obj


# ═══════════════════════════════════════════════════════════════════════════
# FORMAT TRANSLATION: Gemini generateContent → OpenAI Chat Completions
# ═══════════════════════════════════════════════════════════════════════════

def gemini_to_openai(gemini_response, model):
    """Convert a Gemini API response to OpenAI chat completion format."""
    candidates = gemini_response.get('candidates', [])
    text = ""
    finish_reason = "stop"

    if candidates:
        candidate = candidates[0]
        parts = candidate.get('content', {}).get('parts', [])
        text = ''.join(p.get('text', '') for p in parts)

        # Check for function calls in the response
        tool_calls = []
        for p in parts:
            if 'functionCall' in p:
                fc = p['functionCall']
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": fc.get('name', ''),
                        "arguments": json.dumps(fc.get('args', {}))
                    }
                })

        finish_reason = candidate.get('finishReason', 'stop').lower()
        if finish_reason == 'max_tokens':
            finish_reason = 'length'

        if tool_calls:
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": tool_calls
                    },
                    "finish_reason": "tool_calls"
                }],
                "usage": _extract_usage(gemini_response)
            }

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": finish_reason
        }],
        "usage": _extract_usage(gemini_response)
    }


def _extract_usage(gemini_response):
    """Extract token usage from Gemini response."""
    meta = gemini_response.get('usageMetadata', {})
    return {
        "prompt_tokens": meta.get('promptTokenCount', 0),
        "completion_tokens": meta.get('candidatesTokenCount', 0),
        "total_tokens": meta.get('totalTokenCount', 0)
    }


# ═══════════════════════════════════════════════════════════════════════════
# HTTP SERVER
# ═══════════════════════════════════════════════════════════════════════════

class APIHandler(BaseHTTPRequestHandler):
    """HTTP handler exposing OpenAI-compatible endpoints."""

    def do_POST(self):
        if self.path == '/v1/chat/completions':
            self._handle_chat_completions()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == '/v1/models':
            models = [
                {"id": "gemini-3-flash-preview", "object": "model", "owned_by": "google"},
                {"id": "gemini-2.5-flash-preview-05-20", "object": "model", "owned_by": "google"},
                {"id": "gemini-2.5-pro-preview-04-09", "object": "model", "owned_by": "google"}
            ]
            self._json_response(200, {"object": "list", "data": models})
        elif self.path == '/health':
            self._json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def _handle_chat_completions(self):
        """Translate OpenAI request → Gemini → forward to extension → translate back."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length))
        except Exception as e:
            self._json_error(400, f"Invalid JSON: {e}")
            return

        model = body.get('model', 'gemini-3-flash-preview')
        gemini_body = openai_to_gemini(body)
        stream = body.get('stream', False)

        # Create a tracked request
        req_id = str(uuid.uuid4())
        response_queue = Queue()
        pending_requests[req_id] = response_queue

        # Send to extension via native messaging
        send_message({
            "type": "api_request",
            "id": req_id,
            "method": "POST",
            "path": f"/v1beta/models/{model}:generateContent",
            "body": gemini_body,
            "headers": {}
        })

        # Wait for the response (up to 60s)
        try:
            resp = response_queue.get(timeout=60)
        except Empty:
            pending_requests.pop(req_id, None)
            self._json_error(504, "Gateway Timeout — Canvas tab may be closed or unresponsive")
            return

        pending_requests.pop(req_id, None)

        if resp.get('error'):
            self._json_error(502, resp['error'])
            return

        openai_response = gemini_to_openai(resp.get('data', {}), model)

        if stream:
            self._send_streaming(openai_response, model)
        else:
            self._json_response(200, openai_response)

    def _send_streaming(self, openai_response, model):
        """Fake streaming — send as single chunk + [DONE]."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        content = openai_response["choices"][0]["message"]["content"] or ""
        chunk = {
            "id": openai_response["id"],
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
        }
        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

        done_chunk = {
            "id": openai_response["id"],
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        self.wfile.write(f"data: {json.dumps(done_chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json_response(self, code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code, message):
        body = json.dumps({"error": {"message": message, "type": "proxy_error"}}).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Suppress HTTP logs — writing to stdout corrupts native messaging."""
        pass


# ═══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════

def main():
    port = int(os.environ.get('PROXY_PORT', '8765'))

    # Start HTTP server in background thread
    server = HTTPServer(('127.0.0.1', port), APIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Tell the extension we're ready
    send_message({"type": "host_ready", "port": port})

    # Main loop: read messages from the extension
    while True:
        msg = read_message()
        if msg is None:
            break

        if msg.get('type') == 'api_response':
            req_id = msg.get('id')
            if req_id in pending_requests:
                pending_requests[req_id].put({
                    "status": msg.get('status'),
                    "data": msg.get('data'),
                    "error": msg.get('error')
                })

    server.shutdown()


if __name__ == '__main__':
    main()
