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
from socketserver import ThreadingMixIn
from queue import Queue, Empty


# ═══════════════════════════════════════════════════════════════════════════
# THREADED HTTP SERVER
# ═══════════════════════════════════════════════════════════════════════════
# CRITICAL: Must be threaded. When a chat request blocks waiting for the
# extension response (up to 60s), a single-threaded server would block
# ALL other requests including health checks.

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in its own thread."""
    daemon_threads = True


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
payload_store = {}     # request_id → gemini_body (for large payloads that exceed 1MB native messaging limit)
HOST_PORT = 8765       # Set in main(), used by request handlers for payload fetch URLs


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
        # Send native functionCall parts in history. This is the proper Gemini
        # format and avoids the text-encoding mimicry problem where the model
        # copies text tool-call patterns instead of issuing native functionCall.
        if role == 'assistant' and msg.get('tool_calls'):
            parts = []
            if content:
                parts.append({"text": content})
            for tc in msg.get('tool_calls', []):
                func = tc.get('function', {})
                args_str = func.get('arguments', '{}')
                try:
                    args_parsed = json.loads(args_str)
                except Exception:
                    args_parsed = {}
                parts.append({"functionCall": {"name": func.get('name', ''), "args": args_parsed}})
            contents.append({"role": "model", "parts": parts})
            continue

        # ── Tool results ──────────────────────────────────────────────────
        # Send native functionResponse parts. Maps tool_call_id to function name
        # by scanning prior assistant messages.
        if role == 'tool':
            tool_call_id = msg.get('tool_call_id', '')
            func_name = ""
            for prev_msg in body.get('messages', []):
                if prev_msg.get('role') == 'assistant' and prev_msg.get('tool_calls'):
                    for tc in prev_msg['tool_calls']:
                        if tc.get('id') == tool_call_id:
                            func_name = tc.get('function', {}).get('name', '')
                            break
            result_text = str(content)
            if len(result_text) > 5000:
                result_text = result_text[:5000] + "\n... (truncated)"
            # Try to parse as JSON for structured response, else use as string
            try:
                result_obj = json.loads(result_text)
            except Exception:
                result_obj = {"output": result_text}
            contents.append({"role": "function", "parts": [{"functionResponse": {"name": func_name, "response": result_obj}}]})
            continue

        # ── Multimodal content (images, mixed text+image) ────────────────
        # OpenAI sends images as content arrays:
        #   [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:..."}}]
        # Gemini expects separate parts:
        #   [{"text": "..."}, {"inlineData": {"mimeType": "image/png", "data": "base64..."}}]
        #
        # Handles both data URIs and HTTP URLs (fetched server-side).
        # WARNING: Chrome native messaging limits host→extension to 1MB.
        # Large images (>750KB base64) may cause truncation. We log a warning
        # but still attempt delivery — Canvas may accept partial data.
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get('type') == 'text':
                    parts.append({"text": part['text']})
                elif part.get('type') == 'image_url':
                    url = part.get('image_url', {}).get('url', '')
                    if url.startswith('data:'):
                        # Data URI: extract mime type and base64 data
                        meta, b64 = url.split(',', 1)
                        mime = meta.split(';')[0].split(':')[1] if ':' in meta else 'image/jpeg'
                        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
                    elif url.startswith('http'):
                        # HTTP URL: fetch the image server-side, convert to inlineData
                        # This is necessary because Canvas can't fetch arbitrary URLs,
                        # and Gemini's fileData requires a separate upload step that
                        # the Canvas key may not support.
                        try:
                            import urllib.request
                            req = urllib.request.Request(url, headers={'User-Agent': 'GeminiCanvasProxy/1.0'})
                            with urllib.request.urlopen(req, timeout=15) as resp:
                                img_data = resp.read()
                                content_type = resp.headers.get('Content-Type', 'image/jpeg')
                                # Only process if it's actually an image
                                if content_type.startswith('image/'):
                                    import base64
                                    b64 = base64.b64encode(img_data).decode('utf-8')
                                    parts.append({"inlineData": {"mimeType": content_type, "data": b64}})
                        except Exception:
                            # Silently skip failed fetches — don't break the entire request
                            pass
            if parts:
                # Check total payload size for the 1MB native messaging limit
                try:
                    payload_size = len(json.dumps(parts).encode('utf-8'))
                    if payload_size > 900_000:  # 900KB safety margin
                        sys.stderr.write(f"WARNING: Multimodal payload {payload_size}B exceeds 1MB native messaging limit\n")
                except Exception:
                    pass

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
    """
    Convert a Gemini API response to OpenAI chat completion format.

    Handles:
        - Text parts → message.content
        - Image parts (inlineData) → message.content as markdown image data URLs
        - Function calls → tool_calls array
    """
    candidates = gemini_response.get('candidates', [])
    text_parts = []
    image_parts = []
    finish_reason = "stop"

    if candidates:
        candidate = candidates[0]
        parts = candidate.get('content', {}).get('parts', [])

        # Extract text, images, and function calls from parts
        tool_calls = []
        for p in parts:
            if 'text' in p:
                text_parts.append(p['text'])
            elif 'inlineData' in p:
                # Image generation models return images as inlineData
                img_data = p['inlineData'].get('data', '')
                mime = p['inlineData'].get('mimeType', 'image/png')
                image_parts.append(f"![generated_image](data:{mime};base64,{img_data})")

        # Check for function calls
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

        # Combine text and image parts into content
        content = '\n'.join(text_parts + image_parts) or None

        if tool_calls:
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls
                    },
                    "finish_reason": "tool_calls"
                }],
                "usage": _extract_usage(gemini_response)
            }

    content = '\n'.join(text_parts + image_parts) or ""

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
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
                {"id": "gemini-3-flash-preview", "object": "model", "owned_by": "google", "description": "Gemini 3 Flash — fast, capable, great for agents"},
                {"id": "gemini-2.5-flash-preview-05-20", "object": "model", "owned_by": "google", "description": "Gemini 2.5 Flash Preview"},
                {"id": "gemini-3.1-flash-image-preview", "object": "model", "owned_by": "google", "description": "Nano Banana 2 — image generation"},
                {"id": "gemini-2.5-flash-image", "object": "model", "owned_by": "google", "description": "Nano Banana — image generation"}
            ]
            self._json_response(200, {"object": "list", "data": models})
        elif self.path == '/health':
            self._json_response(200, {"status": "ok"})
        elif self.path.startswith('/internal/payload/'):
            # Internal endpoint for extension to fetch large payloads that
            # exceed the 1MB native messaging limit. The extension service
            # worker can fetch() from localhost without LNA restrictions.
            req_id = self.path.split('/internal/payload/')[1]
            body = payload_store.pop(req_id, None)
            if body is not None:
                self._json_response(200, body)
            else:
                self._json_error(404, "Payload not found or already consumed")
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

        # Check if the payload exceeds the 1MB native messaging limit.
        # Chrome's kMaximumNativeMessageSize = 1024*1024 bytes for host→extension.
        # If it does, store the payload and tell the extension to fetch it via HTTP
        # (extension service workers can fetch localhost without LNA restrictions).
        message_payload = {
            "type": "api_request",
            "id": req_id,
            "method": "POST",
            "path": f"/v1beta/models/{model}:generateContent",
            "body": gemini_body,
            "headers": {}
        }
        serialized = json.dumps(message_payload, separators=(',', ':')).encode('utf-8')

        if len(serialized) > 900_000:
            # Payload too large for native messaging (1MB host→extension limit).
            # Strategy: Chunk the payload into <900KB pieces, send each as a
            # separate native messaging message. The extension reassembles them.
            # This works in ALL environments (no HTTP fetch needed, no localhost
            # network access required — pure native messaging).
            chunk_size = 800_000  # 800KB per chunk (safe margin under 1MB)
            payload_str = serialized.decode('utf-8')
            total_chunks = (len(payload_str) + chunk_size - 1) // chunk_size
            sys.stderr.write(f"[Proxy] Large payload ({len(serialized)}B), sending in {total_chunks} chunks\n")
            sys.stderr.flush()

            for i in range(total_chunks):
                chunk = payload_str[i * chunk_size : (i + 1) * chunk_size]
                send_message({
                    "type": "api_request_chunk",
                    "id": req_id,
                    "chunk_index": i,
                    "total_chunks": total_chunks,
                    "chunk_data": chunk
                })
        else:
            # Payload fits within native messaging limit — send inline
            send_message(message_payload)

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
        """Fake streaming — send as single chunk + [DONE].

        Handles both text content and tool_calls in streaming format.
        For tool_calls, we emit each call as a separate delta chunk
        (matching OpenAI's streaming protocol for tool calls), followed
        by a final chunk with finish_reason="tool_calls".
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        choice = openai_response["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")
        tool_calls = message.get("tool_calls")

        if tool_calls:
            # ── Stream tool_calls (OpenAI streaming format) ───────────────
            # Each tool call is sent as a delta with an index. The first
            # chunk includes the role. Arguments may be split across chunks
            # in real OpenAI streaming, but we send them all at once since
            # our proxy fakes streaming (single Gemini response).

            # First chunk: role + first tool call name
            first_tc = tool_calls[0]
            delta = {
                "role": "assistant",
                "tool_calls": [{
                    "index": 0,
                    "id": first_tc["id"],
                    "type": "function",
                    "function": {
                        "name": first_tc["function"]["name"],
                        "arguments": first_tc["function"]["arguments"]
                    }
                }]
            }
            chunk = {
                "id": openai_response["id"],
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

            # Additional tool calls (if multiple)
            for i, tc in enumerate(tool_calls[1:], 1):
                delta = {
                    "tool_calls": [{
                        "index": i,
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"]
                        }
                    }]
                }
                chunk = {
                    "id": openai_response["id"],
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

            # Final chunk: finish_reason = "tool_calls"
            done_chunk = {
                "id": openai_response["id"],
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
            }
            self.wfile.write(f"data: {json.dumps(done_chunk)}\n\n".encode())
        else:
            # ── Stream text content ───────────────────────────────────────
            content = message.get("content") or ""
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
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
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
    global HOST_PORT
    port = int(os.environ.get('PROXY_PORT', '8765'))
    HOST_PORT = port

    # --standalone mode: run HTTP server without native messaging
    # Useful for debugging or when the extension bridge isn't needed.
    # The extension can still connect if it discovers the port.
    standalone = '--standalone' in sys.argv

    # Start HTTP server in a proper thread (not daemon — we want clean shutdown)
    server = ThreadedHTTPServer(('127.0.0.1', port), APIHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    if standalone:
        sys.stderr.write(f"[Proxy] Standalone mode — HTTP server on http://127.0.0.1:{port}\n")
        sys.stderr.write(f"[Proxy] No native messaging — use curl or point any tool at the URL above\n")
        sys.stderr.flush()
        try:
            server_thread.join()
        except KeyboardInterrupt:
            server.shutdown()
        return

    # Tell the extension we're ready
    try:
        send_message({"type": "host_ready", "port": port})
    except Exception:
        # stdout pipe broken — extension may have closed
        sys.stderr.write("[Proxy] Failed to send host_ready, staying alive for HTTP\n")
        sys.stderr.flush()

    # Main loop: read messages from the extension
    while True:
        try:
            msg = read_message()
        except Exception:
            break
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

    # stdin closed — extension disconnected, but keep HTTP server alive
    # for a bit so in-flight requests can complete
    sys.stderr.write("[Proxy] Extension disconnected, HTTP server shutting down\n")
    sys.stderr.flush()
    server.shutdown()


if __name__ == '__main__':
    main()
