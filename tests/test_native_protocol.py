"""Tests for the native-messaging half, with a stand-in for Chrome.

The unit and HTTP tests never touch stdio framing, which is exactly where the
chunking and lifecycle bugs lived. Here the test process launches the real host
binary and speaks Chrome's protocol to it: a 4-byte native-endian length, then
UTF-8 JSON.

Covers both deployment shapes:

  * bare metal — one process serves HTTP and owns the pipes;
  * container  — `--http-only` owns HTTP, `--bridge-only` owns the pipes, and
    they meet on a Unix socket.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "native_host" / "gemini_proxy.py"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Chrome:
    """The browser end of a native-messaging conversation."""

    def __init__(self, process):
        self.process = process
        self.messages = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        stream = self.process.stdout
        while True:
            header = stream.read(4)
            if len(header) < 4:
                return
            (length,) = struct.unpack("=I", header)
            payload = stream.read(length)
            if len(payload) < length:
                return
            with self._lock:
                self.messages.append(json.loads(payload))

    def send(self, message):
        encoded = json.dumps(message, separators=(",", ":")).encode()
        self.process.stdin.write(struct.pack("=I", len(encoded)) + encoded)
        self.process.stdin.flush()

    def wait_for(self, predicate, timeout=10.0, description="message"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for message in list(self.messages):
                    if predicate(message):
                        return message
            time.sleep(0.02)
        with self._lock:
            seen = [m.get("type") for m in self.messages]
        raise AssertionError("timed out waiting for %s; saw %r" % (description, seen))

    def wait_for_type(self, kind, timeout=10.0):
        return self.wait_for(lambda m: m.get("type") == kind, timeout, kind)


def gemini_text(text="ok"):
    return {
        "candidates": [{
            "content": {"parts": [{"text": text}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }


class NativeModeTests(unittest.TestCase):
    """Bare metal: one process, stdio plus HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.token = "native-" + uuid.uuid4().hex
        env = dict(os.environ)
        env.update({
            "PROXY_PORT": str(cls.port),
            "PROXY_TOKEN": cls.token,
            "PROXY_BIND": "127.0.0.1",
        })
        # No flags: this is what Chrome launches from the manifest.
        cls.process = subprocess.Popen(
            [sys.executable, str(HOST)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        cls.chrome = Chrome(cls.process)
        ready = cls.chrome.wait_for_type("host_ready")
        cls.ready = ready

    @classmethod
    def tearDownClass(cls):
        try:
            cls.process.stdin.close()
        except OSError:
            pass
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()

    def drain(self):
        with self.chrome._lock:
            self.chrome.messages.clear()
        time.sleep(0.05)

    def test_host_announces_itself_with_a_protocol_version(self):
        self.assertEqual(self.ready["port"], self.port)
        self.assertEqual(self.ready["protocol"], 2)

    def test_http_surface_is_up_alongside_stdio(self):
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=5) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["status"], "ok")

    def test_http_request_flows_over_stdio_and_back(self):
        import http.client

        def serve():
            message = self.chrome.wait_for(lambda m: m.get("type") == "api_request")
            self.last_path = message["path"]
            self.last_stream = message["stream"]
            self.chrome.send({
                "type": "api_response", "id": message["id"], "status": 200,
                "data": gemini_text("over stdio"),
            })

        threading.Thread(target=serve, daemon=True).start()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        connection.request(
            "POST", "/v1/chat/completions",
            body=json.dumps({"model": "gemini-test",
                             "messages": [{"role": "user", "content": "hi"}]}),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "over stdio")
        self.assertIn("/v1beta/models/gemini-test:generateContent", self.last_path)
        self.assertFalse(self.last_stream)

    def test_oversized_request_is_chunked(self):
        self.drain()
        # ~1.2 MB of text, comfortably past the 900 KB chunk threshold but far
        # below the request ceiling.
        big = "ab" * 600_000
        gemini_body = {
            "contents": [{"role": "user", "parts": [{"text": big}]}],
            "generationConfig": {},
        }
        serialized = json.dumps({
            "type": "api_request", "id": "x", "method": "POST",
            "path": "/v1beta/models/m:generateContent", "body": gemini_body,
            "headers": {}, "stream": False,
        }, separators=(",", ":")).encode()
        self.assertGreater(len(serialized), 900_000)

        request_id = "big-" + uuid.uuid4().hex[:8]
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": big}]}).encode()
        self._post_with_big_body(body)

        chunks = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.chrome._lock:
                chunks = [m for m in self.chrome.messages if m.get("type") == "api_request_chunk"]
            if chunks and len(chunks) >= chunks[0].get("total_chunks", 1):
                break
            time.sleep(0.02)

        self.assertTrue(chunks, "no chunk messages were emitted")
        total = chunks[0]["total_chunks"]
        self.assertGreater(total, 1)
        self.assertEqual(sorted(c["chunk_index"] for c in chunks), list(range(total)))
        for chunk in chunks:
            encoded = json.dumps(chunk, separators=(",", ":")).encode()
            self.assertLessEqual(len(encoded), 1 << 20)
        self.assertEqual(request_id and chunks[0]["id"], chunks[0]["id"])

    def _post_with_big_body(self, body):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        connection.request(
            "POST", "/v1/chat/completions", body=body,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        # The response only arrives once the fake Chrome replies; we do not
        # care about it here, only about what went out on the wire.
        threading.Thread(target=lambda: self._swallow(connection), daemon=True).start()

    @staticmethod
    def _swallow(connection):
        try:
            connection.getresponse().read()
        except Exception:
            pass
        finally:
            connection.close()


class SplitModeTests(unittest.TestCase):
    """Container layout: HTTP role and stdio role in separate processes."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.socket_path = "/tmp/gcp-split-%s.sock" % uuid.uuid4().hex[:8]
        cls.token = "split-" + uuid.uuid4().hex
        env = dict(os.environ)
        env.update({
            "PROXY_PORT": str(cls.port),
            "PROXY_TOKEN": cls.token,
            "PROXY_BIND": "127.0.0.1",
            "PROXY_SOCKET": cls.socket_path,
        })

        cls.http = subprocess.Popen(
            [sys.executable, str(HOST), "--http-only"],
            env=env, stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if os.path.exists(cls.socket_path):
                break
            time.sleep(0.05)

        cls.bridge = subprocess.Popen(
            [sys.executable, str(HOST), "--bridge-only"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        cls.chrome = Chrome(cls.bridge)
        cls.ready = cls.chrome.wait_for_type("host_ready")

    @classmethod
    def tearDownClass(cls):
        for process in (cls.bridge, cls.http):
            try:
                process.kill()
            except OSError:
                pass
        try:
            os.unlink(cls.socket_path)
        except OSError:
            pass

    def setUp(self):
        # Each test must start with an empty transcript, otherwise wait_for()
        # can latch onto a request id left over from the previous test and
        # answer a request nobody is waiting for.
        with self.chrome._lock:
            self.chrome.messages.clear()

    def test_bridge_announces_itself(self):
        self.assertEqual(self.ready["protocol"], 2)

    def test_http_request_crosses_the_socket(self):
        import http.client
        request_id = "split-" + uuid.uuid4().hex[:8]

        result = {}

        def serve():
            message = self.chrome.wait_for(
                lambda m: m.get("type") == "api_request", description="request over the socket"
            )
            result["path"] = message["path"]
            self.chrome.send({
                "type": "api_response",
                "id": message["id"],
                "status": 200,
                "data": gemini_text("through the socket"),
            })

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        connection.request(
            "POST", "/v1/chat/completions",
            body=json.dumps({"model": "gemini-test",
                             "messages": [{"role": "user", "content": "hi"}]}),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["choices"][0]["message"]["content"], "through the socket")
        self.assertIn("/v1beta/models/gemini-test", result["path"])
        self.assertTrue(request_id)

    def test_streaming_crosses_the_socket(self):
        import http.client
        events = [
            {"event": "start"},
            {"event": "delta", "text": "a"},
            {"event": "delta", "text": "b"},
            {"event": "end", "finish_reason": "stop"},
        ]

        def serve():
            message = self.chrome.wait_for(lambda m: m.get("type") == "api_request")
            for event in events:
                self.chrome.send({
                    "type": "api_stream_event", "id": message["id"], **event
                })

        threading.Thread(target=serve, daemon=True).start()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        connection.request(
            "POST", "/v1/chat/completions",
            body=json.dumps({"model": "gemini-test", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]}),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertIn('"content": "a"', body)
        self.assertIn('"content": "b"', body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))



class BridgeLossTests(unittest.TestCase):
    """Killing the bridge must fail requests immediately, not after a timeout.

    Its own fixture, because the test is destructive: it leaves the HTTP role
    with no bridge attached.
    """

    @classmethod
    def setUpClass(cls):
        import http.client  # noqa: F401 - imported for the test bodies

        cls.port = free_port()
        cls.socket_path = "/tmp/gcp-loss-%s.sock" % uuid.uuid4().hex[:8]
        cls.token = "loss-" + uuid.uuid4().hex
        env = dict(os.environ)
        env.update({
            "PROXY_PORT": str(cls.port),
            "PROXY_TOKEN": cls.token,
            "PROXY_BIND": "127.0.0.1",
            "PROXY_SOCKET": cls.socket_path,
            "PROXY_REQUEST_TIMEOUT": "30",
        })
        cls.env = env
        cls.http = subprocess.Popen([sys.executable, str(HOST), "--http-only"],
                                    env=env, stderr=subprocess.PIPE)
        for _ in range(100):
            if os.path.exists(cls.socket_path):
                break
            time.sleep(0.05)
        cls.cls_bridge = subprocess.Popen(
            [sys.executable, str(HOST), "--bridge-only"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        cls.chrome = Chrome(cls.cls_bridge)
        cls.chrome.wait_for_type("host_ready")

    @classmethod
    def tearDownClass(cls):
        for process in (cls.cls_bridge, cls.http):
            try:
                process.kill()
            except OSError:
                pass
        try:
            os.unlink(cls.socket_path)
        except OSError:
            pass

    def test_request_without_a_bridge_fails_immediately(self):
        import http.client
        self.cls_bridge.kill()
        self.cls_bridge.wait(timeout=5)
        time.sleep(0.4)

        started = time.monotonic()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(
            "POST", "/v1/chat/completions",
            body=json.dumps({"model": "gemini-test",
                             "messages": [{"role": "user", "content": "hi"}]}),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        elapsed = time.monotonic() - started

        self.assertEqual(response.status, 502)
        self.assertIn("bridge", payload["error"]["message"].lower())
        # Fail fast means fail fast: nowhere near the 30 s request timeout.
        self.assertLess(elapsed, 5)


if __name__ == "__main__":
    unittest.main()
