"""End-to-end tests for the HTTP surface, driven through a stub bridge.

No browser involved: the proxy's `--http-only` role is started in-process and a
stub speaks the Unix-socket protocol on the other end. That covers everything
between the socket and the wire, which is where most of the bugs in this project
have lived.
"""

import http.client
import importlib.util
import json
import os
import socket
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gemini_proxy_http", ROOT / "native_host" / "gemini_proxy.py")
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)

TOKEN = "test-token-" + uuid.uuid4().hex


# ── stub bridge ────────────────────────────────────────────────────────────

class StubBridge:
    """Connects to the proxy's socket and replies however the test wants."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.handler = None
        self.requests = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def wait_for_request(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.requests:
                return self.requests[-1]
            time.sleep(0.02)
        raise AssertionError("stub bridge saw no request")

    def _run(self):
        while not self._stop.is_set():
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(self.socket_path)
            except OSError:
                connection.close()
                time.sleep(0.02)
                continue

            with connection:
                while not self._stop.is_set():
                    try:
                        message = self._read(connection)
                    except OSError:
                        break
                    if message is None:
                        break
                    self.requests.append(message)
                    if self.handler:
                        self.handler(self, connection, message)
            time.sleep(0.02)

    @staticmethod
    def _read(connection):
        header = b""
        while len(header) < 4:
            chunk = connection.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        (length,) = __import__("struct").unpack("=I", header)
        body = b""
        while len(body) < length:
            chunk = connection.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body)

    @staticmethod
    def send(connection, message):
        encoded = json.dumps(message, separators=(",", ":")).encode()
        connection.sendall(__import__("struct").pack("=I", len(encoded)) + encoded)


def reply_with(gemini_payload, status=200):
    def handler(bridge, connection, message):
        StubBridge.send(connection, {
            "type": "api_response",
            "id": message["id"],
            "status": status,
            "data": gemini_payload,
        })
    return handler


def stream_with(events):
    def handler(bridge, connection, message):
        for event in events:
            StubBridge.send(connection, {
                "type": "api_stream_event",
                "id": message["id"],
                **event,
            })
    return handler


# ── harness ────────────────────────────────────────────────────────────────

HARNESS = {}


def setUpModule():
    """One server, one bridge, one socket for the whole module.

    Per-class fixtures were the source of a flake: tearing down a class left a
    stub reconnecting to a unlinked socket while the next class installed a new
    one, and occasionally a request landed on the dying connection.
    """
    socket_path = "/tmp/gcp-test-%s.sock" % uuid.uuid4().hex[:8]
    os.environ["PROXY_TOKEN"] = TOKEN
    os.environ["PROXY_SOCKET"] = socket_path
    os.environ["PROXY_REQUEST_TIMEOUT"] = "10"

    proxy.outbound_sender = proxy.send_to_bridge
    proxy.pending_requests.clear()

    threading.Thread(target=proxy.serve_bridge_socket, args=(socket_path,), daemon=True).start()

    server = proxy.ThreadedHTTPServer(("127.0.0.1", 0), proxy.APIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    for _ in range(200):
        if os.path.exists(socket_path):
            break
        time.sleep(0.01)

    bridge = StubBridge(socket_path).start()
    HARNESS.update(server=server, bridge=bridge, socket_path=socket_path,
                   port=server.server_address[1])


def tearDownModule():
    HARNESS["bridge"].stop()
    HARNESS["server"].shutdown()
    try:
        os.unlink(HARNESS["socket_path"])
    except OSError:
        pass


class ProxyTestCase(unittest.TestCase):
    port = 0

    @classmethod
    def setUpClass(cls):
        cls.port = HARNESS["port"]
        cls.bridge = HARNESS["bridge"]

    def request(self, method, path, body=None, token=TOKEN, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        all_headers = {"Content-Type": "application/json"}
        if token:
            all_headers["Authorization"] = "Bearer " + token
        all_headers.update(headers or {})

        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()

        connection.request(method, path, body=payload, headers=all_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response, raw

    def post_json(self, body, **kwargs):
        status, response, raw = self.request("POST", "/v1/chat/completions", body, **kwargs)
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return status, parsed, response

    def setUp(self):
        proxy.outbound_sender = proxy.send_to_bridge
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with proxy.bridge_lock:
                if proxy.bridge_connection is not None:
                    break
            time.sleep(0.02)
        else:
            self.fail("stub bridge is not attached")

        self.bridge.handler = reply_with({
            "candidates": [{
                "content": {"parts": [{"text": "hello"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7},
        })
        self.bridge.requests.clear()


# ── tests ──────────────────────────────────────────────────────────────────

class HealthAndAuthTests(ProxyTestCase):
    def test_health_is_unauthenticated_and_reports_bridge(self):
        status, _, raw = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["bridge"]["connected"])

    def test_models_requires_a_token(self):
        status, _, _ = self.request("GET", "/v1/models", token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_is_rejected(self):
        status, _, _ = self.request("GET", "/v1/models", token="nope")
        self.assertEqual(status, 401)

    def test_models_lists_catalog(self):
        status, _, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["data"])

    def test_model_by_id(self):
        status, _, raw = self.request("GET", "/v1/models/gemini-3-flash-preview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["id"], "gemini-3-flash-preview")

    def test_unknown_model_by_id_is_404_json(self):
        status, _, raw = self.request("GET", "/v1/models/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(raw))

    def test_unknown_path_is_json_404(self):
        status, _, raw = self.request("GET", "/v1/nothing-here")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(raw))

    def test_status_endpoint(self):
        status, _, raw = self.request("GET", "/status")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["protocol"], proxy.PROTOCOL_VERSION)
        self.assertIn("bridge", payload)


class ChatCompletionTests(ProxyTestCase):
    def test_basic_completion(self):
        status, payload, _ = self.post_json({
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello")
        self.assertEqual(payload["usage"]["total_tokens"], 7)

    def test_http_version_is_1_1(self):
        status, response, _ = self.request("GET", "/health", token=None)
        self.assertEqual(response.version, 11)

    def test_invalid_json_is_400(self):
        status, payload, _ = self.post_json(b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_invalid_model_is_400(self):
        status, payload, _ = self.post_json({
            "model": "../../etc/passwd",
            "messages": [{"role": "user", "content": "x"}],
        })
        self.assertEqual(status, 400)

    def test_missing_body_is_400(self):
        status, payload, _ = self.post_json(None)
        self.assertEqual(status, 400)

    def test_missing_content_length_is_411(self):
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        connection.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
            b"\r\n"
        )
        head = connection.recv(4096)
        connection.close()
        self.assertIn(b"411", head.split(b"\r\n")[0])

    def test_chunked_body_is_accepted(self):
        body = json.dumps({"model": "gemini-3-flash-preview", "messages": [{"role": "user", "content": "hi"}]}).encode()
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        head = (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        connection.sendall(head)
        connection.sendall(b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
        response = connection.recv(4096)
        connection.close()
        self.assertIn(b"200", response.split(b"\r\n")[0])

    def test_oversize_body_is_413(self):
        os.environ["PROXY_MAX_REQUEST_BYTES"] = "64"
        try:
            status, payload, _ = self.post_json({
                "model": "gemini-3-flash-preview",
                "messages": [{"role": "user", "content": "x" * 500}],
            })
        finally:
            del os.environ["PROXY_MAX_REQUEST_BYTES"]
        self.assertEqual(status, 413)

    def test_blocked_prompt_surfaces_details(self):
        self.bridge.handler = reply_with({
            "promptFeedback": {"blockReason": "SAFETY", "safetyRatings": [{"blocked": True}]}
        })
        status, payload, _ = self.post_json({
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "x"}],
        })
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["details"]["blockReason"], "SAFETY")

    def test_bridge_error_is_502(self):
        def handler(bridge, connection, message):
            StubBridge.send(connection, {
                "type": "api_response", "id": message["id"], "error": "no canvas tab"
            })
        self.bridge.handler = handler
        status, payload, _ = self.post_json({
            "model": "gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "x"}],
        })
        self.assertEqual(status, 502)
        self.assertIn("no canvas tab", payload["error"]["message"])

    def test_timeout_is_504(self):
        self.bridge.handler = lambda *_: None  # never answer
        os.environ["PROXY_REQUEST_TIMEOUT"] = "1"
        try:
            status, payload, _ = self.post_json({
                "model": "gemini-3-flash-preview",
                "messages": [{"role": "user", "content": "x"}],
            })
        finally:
            os.environ["PROXY_REQUEST_TIMEOUT"] = "10"
        self.assertEqual(status, 504)

    def test_tool_result_history_survives(self):
        self.bridge.handler = reply_with({
            "candidates": [{
                "content": {"parts": [
                    {"functionCall": {"name": "lookup", "args": {"q": 1}},
                     "thoughtSignature": "sig"},
                ]},
                "finishReason": "STOP",
            }]
        })
        status, payload, _ = self.post_json({
            "model": "gemini-3-flash-preview",
            "messages": [
                {"role": "user", "content": "find"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "lookup", "arguments": "{\"q\":1}"}}
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\":true}"},
            ],
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(
            payload["choices"][0]["message"]["tool_calls"][0]["x_gemini_thought_signature"], "sig"
        )


class StreamingTests(ProxyTestCase):
    def read_stream(self):
        status, response, raw = self.request("POST", "/v1/chat/completions", {
            "model": "gemini-3-flash-preview",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        return status, raw.decode()

    def test_stream_emits_deltas_in_order_then_done(self):
        self.bridge.handler = stream_with([
            {"event": "start"},
            {"event": "delta", "text": "Hel"},
            {"event": "delta", "text": "lo"},
            {"event": "end", "finish_reason": "stop",
             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        ])
        status, body = self.read_stream()
        self.assertEqual(status, 200)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))

        events = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        text = "".join(
            event["choices"][0]["delta"].get("content", "")
            for event in events
            if event.get("choices")
        )
        self.assertEqual(text, "Hello")
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")

    def test_stream_include_usage_adds_final_usage_chunk(self):
        self.bridge.handler = stream_with([
            {"event": "start"},
            {"event": "delta", "text": "x"},
            {"event": "end", "finish_reason": "stop",
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ])
        status, response, raw = self.request("POST", "/v1/chat/completions", {
            "model": "gemini-3-flash-preview",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        events = [
            json.loads(line[len("data: "):])
            for line in raw.decode().splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(events[-1]["usage"]["total_tokens"], 2)

    def test_stream_tool_call_delta(self):
        self.bridge.handler = stream_with([
            {"event": "start"},
            {"event": "tool_call", "tool_call": {
                "index": 0, "id": "call_1", "name": "lookup", "arguments": "{\"q\":1}"
            }},
            {"event": "end", "finish_reason": "tool_calls"},
        ])
        status, body = self.read_stream()
        self.assertEqual(status, 200)
        self.assertIn('"tool_calls"', body)
        self.assertIn('"finish_reason": "tool_calls"', body)

    def test_stream_error_before_start_is_json(self):
        self.bridge.handler = stream_with([
            {"event": "error", "error": "canvas went away"},
        ])
        status, payload, _ = self.post_json({
            "model": "gemini-3-flash-preview",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 502)
        self.assertIn("canvas went away", payload["error"]["message"])


class BridgeLifecycleTests(ProxyTestCase):
    def test_no_bridge_gives_a_clear_502(self):
        proxy.outbound_sender = proxy.send_to_bridge
        saved = proxy.bridge_connection
        with proxy.bridge_lock:
            proxy.bridge_connection = None
        try:
            status, payload, _ = self.post_json({
                "model": "gemini-3-flash-preview",
                "messages": [{"role": "user", "content": "x"}],
            })
        finally:
            with proxy.bridge_lock:
                proxy.bridge_connection = saved
        self.assertEqual(status, 502)
        self.assertIn("bridge", payload["error"]["message"].lower())

    def test_health_reports_disconnected_bridge(self):
        saved = proxy.bridge_connection
        with proxy.bridge_lock:
            proxy.bridge_connection = None
        try:
            status, _, raw = self.request("GET", "/health", token=None)
        finally:
            with proxy.bridge_lock:
                proxy.bridge_connection = saved
        self.assertFalse(json.loads(raw)["bridge"]["connected"])


if __name__ == "__main__":
    unittest.main()
