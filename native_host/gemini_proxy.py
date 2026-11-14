#!/usr/bin/env python3
"""
Gemini Canvas Proxy — native messaging host.

Serves an OpenAI-compatible HTTP API on localhost and forwards every request
through Chrome to a Gemini Canvas page, which is where the credentials live.

Why three processes' worth of plumbing: Chrome only lets an extension talk to a
native binary it launched itself. That binary therefore has to be the one
holding stdio, but the HTTP listener has to survive independently of any single
browser tab. The roles below split those two responsibilities.

    --http-only     Owns the TCP listener and a Unix socket. Started by the
                    container entrypoint, or by hand for debugging with a test
                    bridge. Accepts exactly one bridge connection at a time.
    --bridge-only   Launched by Chrome through the native-messaging manifest in
                    the container, where /app is read-only and the HTTP role
                    already exists. Pumps stdio to the Unix socket.
    (no flag)       Launched by Chrome on bare metal. Owns stdio *and* serves
                    HTTP, so a desktop install needs only one process.

Wire format in both directions is Chrome's native messaging framing: a 4-byte
native-endian length followed by UTF-8 JSON. Chrome caps host-to-browser
messages at 1 MiB, so anything larger is split into base64 chunks; the
browser-to-host direction allows 64 MiB and is never chunked.
"""

import argparse
import base64
import hmac
import ipaddress
import json
import os
import re
import socket
import stat
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from socketserver import ThreadingMixIn

PROTOCOL_VERSION = 2

# Chrome's native messaging limits, in bytes.
MAX_HOST_TO_BROWSER_BYTES = 1 << 20
MAX_BROWSER_TO_HOST_BYTES = 64 << 20

# Base64 inflates by 4/3. 600 KB of raw bytes encodes to 800 KB, leaving room
# for the JSON envelope inside the 1 MiB frame.
RAW_CHUNK_BYTES = 600_000
MAX_CHUNKS = 128

# Chunking is the only way to move a large request host -> browser, so the
# request ceiling cannot exceed what MAX_CHUNKS can carry.
MAX_CHUNKED_REQUEST_BYTES = RAW_CHUNK_BYTES * MAX_CHUNKS

DEFAULT_PORT = 8765
DEFAULT_REQUEST_TIMEOUT = 180.0

# Media models are not slower to start, they are slower to finish: an image is
# delivered once, after the whole thing has been drawn. A text-sized ceiling
# turns every image request into a 504 on a tab that is working fine.
DEFAULT_MEDIA_TIMEOUT = 600.0
MEDIA_MODEL_HINTS = ("image", "tts")
DEFAULT_MAX_REQUEST_BYTES = 32 << 20
DEFAULT_TOOL_RESULT_CHARS = 5_000
DEFAULT_MAX_IMAGE_BYTES = 20 << 20

MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
GEMINI_PATH_PATTERN = re.compile(
    r"^/v1beta/models/([A-Za-z0-9._-]+):(generateContent|streamGenerateContent)$"
)

FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "LANGUAGE": "content_filter",
    "OTHER": "stop",
    "MALFORMED_FUNCTION_CALL": "stop",
    "UNEXPECTED_TOOL_CALL": "stop",
}

# Gemini's functionDeclarations accept a strict subset of JSON Schema. Sending
# anything else produces MALFORMED_FUNCTION_CALL, which surfaces to the caller
# as an empty response — the least debuggable failure mode Gemini has.
UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "$schema", "$id", "$ref", "$defs", "$comment",
    "additionalProperties", "unevaluatedProperties", "unevaluatedItems",
    "patternProperties", "dependentRequired", "dependentSchemas",
    "propertyNames", "contentMediaType", "contentEncoding",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    "const", "examples", "default", "nullable", "title", "deprecated",
    "readOnly", "writeOnly",
    "maxLength", "minLength", "pattern", "format",
    "maxItems", "minItems", "uniqueItems", "contains", "minContains", "maxContains",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
})


# ───────────────────────────────────────────────────────────────────────────
# Logging
# ───────────────────────────────────────────────────────────────────────────
# Everything goes to stderr. On stdout, a stray print would be read by Chrome
# as a malformed native message and tear down the port.

def log(message, *args):
    """Write to stderr and to the host log.

    Chrome hands the host a stderr pipe that nothing reads, so a line written
    only there is gone. The file is the only place a user can look when the
    browser is the thing misbehaving.
    """
    if args:
        message = message % args
    sys.stderr.write("[proxy] %s\n" % message)
    sys.stderr.flush()
    append_log_line(message)


def warn(message, *args):
    log("WARN " + (message % args if args else message))


# ───────────────────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────────────────

class ConfigError(RuntimeError):
    """An environment variable is present but unusable."""


def env_int(name, default, minimum=None, maximum=None):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("%s must be an integer, got %r" % (name, raw))
    if minimum is not None and value < minimum:
        raise ConfigError("%s must be >= %s, got %s" % (name, minimum, value))
    if maximum is not None and value > maximum:
        raise ConfigError("%s must be <= %s, got %s" % (name, maximum, value))
    return value


def env_float(name, default, minimum=None):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("%s must be a number, got %r" % (name, raw))
    if minimum is not None and value < minimum:
        raise ConfigError("%s must be >= %s, got %s" % (name, minimum, value))
    return value


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def host_log_path():
    """Where a fatal startup error is written.

    Chrome gives the host a stderr pipe that nothing reads, so a crash there
    leaves no trace at all — the only visible symptom is the browser restarting
    the process forever. This file is the difference between a crash loop and a
    bug report.
    """
    return os.environ.get(
        "PROXY_LOG_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".host.log"),
    )


def append_log_line(detail):
    """Append one timestamped line to the host log, rotating at 64 KiB."""
    try:
        path = host_log_path()
        try:
            if os.path.getsize(path) > 64 * 1024:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s  pid=%d  %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), detail))
    except OSError:
        pass


def record_fatal(detail):
    append_log_line(detail)


def token_file_path():
    return os.environ.get(
        "PROXY_TOKEN_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".proxy_token"),
    )


def load_proxy_token():
    """Bearer token from the environment, falling back to the 0600 token file."""
    token = os.environ.get("PROXY_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(token_file_path(), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


# ───────────────────────────────────────────────────────────────────────────
# Native messaging framing
# ───────────────────────────────────────────────────────────────────────────

def read_message():
    """Read one framed message from stdin. Returns None at end of stream."""
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    if length == 0:
        return None
    if length > MAX_BROWSER_TO_HOST_BYTES:
        raise ValueError(
            "native message claims %d bytes, above the %d-byte limit"
            % (length, MAX_BROWSER_TO_HOST_BYTES)
        )
    payload = sys.stdin.buffer.read(length)
    if len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def send_message(message):
    """Write one framed message to stdout without exceeding Chrome's limit."""
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_HOST_TO_BROWSER_BYTES:
        raise ValueError(
            "native message is %d bytes, above the %d-byte limit"
            % (len(encoded), MAX_HOST_TO_BROWSER_BYTES)
        )
    sys.stdout.buffer.write(struct.pack("=I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def recv_exact(connection, length):
    chunks = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_socket_message(connection):
    header = recv_exact(connection, 4)
    if header is None:
        return None
    (length,) = struct.unpack("=I", header)
    if length == 0:
        return None
    if length > MAX_BROWSER_TO_HOST_BYTES:
        raise ValueError("unix-socket frame too large: %d bytes" % length)
    payload = recv_exact(connection, length)
    if payload is None:
        return None
    return json.loads(payload.decode("utf-8"))


def write_socket_message(connection, message):
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
    connection.sendall(struct.pack("=I", len(encoded)) + encoded)


def chunk_request(serialized, request_id):
    """Split a serialized request into base64 native-message envelopes."""
    pieces = [
        serialized[offset:offset + RAW_CHUNK_BYTES]
        for offset in range(0, len(serialized), RAW_CHUNK_BYTES)
    ]
    if len(pieces) > MAX_CHUNKS:
        raise ValueError(
            "request needs %d native messages, above the %d limit; "
            "lower PROXY_MAX_REQUEST_BYTES"
            % (len(pieces), MAX_CHUNKS)
        )

    messages = []
    for index, piece in enumerate(pieces):
        message = {
            "type": "api_request_chunk",
            "id": request_id,
            "chunk_index": index,
            "total_chunks": len(pieces),
            "chunk_encoding": "base64",
            "chunk_data": base64.b64encode(piece).decode("ascii"),
        }
        size = len(json.dumps(message, separators=(",", ":")).encode("utf-8"))
        if size > MAX_HOST_TO_BROWSER_BYTES:
            raise ValueError("chunk envelope is %d bytes, above the limit" % size)
        messages.append(message)
    return messages


# ───────────────────────────────────────────────────────────────────────────
# Bridge routing
# ───────────────────────────────────────────────────────────────────────────
# Each in-flight HTTP request owns a Queue. The bridge reader thread pushes
# events into the matching queue and never blocks on a slow client.

# How this process reaches the browser: "native" when Chrome launched us and we
# own stdio, "socket" when we are the --http-only half of the container split.
# /health has to report the difference, because "no socket connected" means
# something completely different in each case.
transport = "native"

pending_requests = {}
pending_lock = threading.Lock()
bridge_connection = None
bridge_connected_at = None
bridge_lock = threading.Lock()
outbound_sender = send_message


def register_request(request_id):
    queue = Queue()
    with pending_lock:
        pending_requests[request_id] = queue
    return queue


def unregister_request(request_id):
    with pending_lock:
        pending_requests.pop(request_id, None)


def route_bridge_message(message):
    message_type = message.get("type")
    # Every message the extension sends arrives here. One line per message is
    # what makes "the page answered but nothing came back" answerable from the
    # log instead of a service-worker console.
    log("bridge message: %s id=%s", message_type, message.get("id") or "-")
    if message_type == "ext_status":
        request_id = message.get("id")
        with pending_lock:
            queue = pending_requests.get(request_id)
        if queue is not None:
            queue.put({"kind": "ext_status", "data": message.get("data") or {}})
        return
    request_id = message.get("id")
    if not request_id:
        return
    with pending_lock:
        queue = pending_requests.get(request_id)
    if queue is None:
        # The HTTP client already gave up. Dropping is correct: there is
        # nobody left to deliver to, and the extension should not retry.
        return

    if message_type == "api_response":
        queue.put({
            "kind": "response",
            "status": message.get("status", 200),
            "data": message.get("data"),
            "error": message.get("error"),
        })
    elif message_type == "api_stream_event":
        queue.put({
            "kind": "stream",
            "event": message.get("event"),
            "text": message.get("text"),
            "tool_call": message.get("tool_call"),
            "finish_reason": message.get("finish_reason"),
            "usage": message.get("usage"),
            "error": message.get("error"),
        })


def send_to_bridge(message):
    with bridge_lock:
        connection = bridge_connection
    if connection is None:
        raise ConnectionError(
            "no bridge connected — is the extension loaded and the Canvas page open?"
        )
    write_socket_message(connection, message)


def serve_bridge_socket(socket_path):
    """Accept one bridge connection at a time on a mode-0600 Unix socket."""
    global bridge_connection, bridge_connected_at

    if os.path.lexists(socket_path):
        if not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
            raise RuntimeError(
                "refusing to replace %s: it exists and is not a socket" % socket_path
            )
        os.unlink(socket_path)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(socket_path)
    os.chmod(socket_path, 0o600)
    listener.listen(1)
    log("bridge socket listening at %s", socket_path)

    while True:
        connection, _ = listener.accept()
        log("bridge connected")
        with bridge_lock:
            previous = bridge_connection
            bridge_connection = connection
            bridge_connected_at = time.monotonic()
        if previous is not None:
            # A second browser (or a restart) supersedes the old one. Closing
            # the stale socket is what stops two Chromium instances fighting.
            try:
                previous.close()
            except OSError:
                pass

        try:
            while True:
                message = read_socket_message(connection)
                if message is None:
                    break
                route_bridge_message(message)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the listener
            warn("bridge read failed: %s", exc)
        finally:
            with bridge_lock:
                if bridge_connection is connection:
                    bridge_connection = None
                    bridge_connected_at = None
            try:
                connection.close()
            except OSError:
                pass
            log("bridge disconnected")


def connect_bridge_socket(socket_path, timeout=15.0):
    deadline = time.monotonic() + timeout
    while True:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(socket_path)
            return connection
        except OSError:
            connection.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


# ───────────────────────────────────────────────────────────────────────────
# Remote image fetching
# ───────────────────────────────────────────────────────────────────────────

class ImageFetchError(ValueError):
    """A remote image was rejected or could not be fetched safely."""


def validate_public_url(url):
    """Reject anything that is not a plain public HTTP(S) destination."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ImageFetchError("only HTTP and HTTPS URLs are supported")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ImageFetchError("URL authority is invalid")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ImageFetchError("URL port is invalid") from exc
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ImageFetchError("hostname resolution failed: %s" % exc) from exc
    if not addresses:
        raise ImageFetchError("hostname did not resolve")

    for family_info in addresses:
        address = ipaddress.ip_address(family_info[4][0])
        if not address.is_global or address.is_multicast:
            raise ImageFetchError("destination resolves to a non-public address")
    return parsed


class PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-apply the destination policy on every redirect hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_remote_image(url):
    if not env_flag("PROXY_ALLOW_URL_FETCH"):
        raise ImageFetchError(
            "remote URL fetching is disabled; send a data URI or set "
            "PROXY_ALLOW_URL_FETCH=true"
        )

    validate_public_url(url)
    max_bytes = env_int("PROXY_MAX_IMAGE_BYTES", DEFAULT_MAX_IMAGE_BYTES, minimum=1)

    request = urllib.request.Request(url, headers={"User-Agent": "gemini-canvas-proxy"})
    opener = urllib.request.build_opener(PublicOnlyRedirectHandler())

    try:
        with opener.open(request, timeout=15) as response:
            content_type = (
                response.headers.get("Content-Type", "application/octet-stream")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if not content_type.startswith("image/"):
                raise ImageFetchError(
                    "response content type is not an image (%s)" % content_type
                )

            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    declared_bytes = int(declared)
                except ValueError as exc:
                    raise ImageFetchError("response Content-Length is invalid") from exc
                if declared_bytes > max_bytes:
                    raise ImageFetchError("image exceeds the %d-byte limit" % max_bytes)

            chunks = []
            received = 0
            while True:
                chunk = response.read(min(64 * 1024, max_bytes - received + 1))
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise ImageFetchError("image exceeds the %d-byte limit" % max_bytes)
                chunks.append(chunk)
    except ImageFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - network stacks raise a wide variety
        raise ImageFetchError("fetch failed: %s" % exc) from exc

    return content_type, base64.b64encode(b"".join(chunks)).decode("ascii")


def log_dropped_image(url, reason):
    try:
        host = urllib.parse.urlsplit(url).hostname or "<invalid-host>"
    except ValueError:
        host = "<invalid-host>"
    warn("dropped remote image from %s: %s", host, reason)


# ───────────────────────────────────────────────────────────────────────────
# Request translation: OpenAI chat completions -> Gemini generateContent
# ───────────────────────────────────────────────────────────────────────────

class RequestError(ValueError):
    """The caller's request cannot be translated. Surfaces as HTTP 400."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def pcm_to_wav(pcm, sample_rate=24000, channels=1, sample_width=2):
    """Wrap raw PCM in a RIFF header so the result is playable anywhere.

    Gemini's speech models return `audio/L16;codec=pcm;rate=24000`, which is a
    headerless byte stream. Handing that to a client means asking it to know the
    rate, the channel count and the endianness; a WAV is self-describing and
    plays in a browser tab.
    """
    byte_rate = sample_rate * channels * sample_width
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate,
        channels * sample_width, sample_width * 8,
    )
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def audio_sample_rate(mime):
    """Pull `rate=` out of a Gemini audio MIME type, defaulting to 24 kHz."""
    match = re.search(r"rate=(\d+)", mime or "")
    return int(match.group(1)) if match else 24000


def is_audio(mime):
    return (mime or "").lower().startswith("audio/")


# Gemini accepts inline parts for these. Anything else has to go through the
# Files API, which the Canvas key does not expose.
INLINE_MIME_PREFIXES = ("image/", "audio/", "video/")
INLINE_MIME_EXACT = {"application/pdf"}

# One inline part has to survive base64 (4/3) and the native-messaging frame.
MAX_INLINE_BYTES = 18 * 1024 * 1024


def is_inline_media(mime):
    return mime.startswith(INLINE_MIME_PREFIXES) or mime in INLINE_MIME_EXACT


def parse_data_uri(url):
    """Return (mime, base64) from a data: URI, or raise RequestError."""
    header, separator, payload = url.partition(",")
    if not separator:
        raise RequestError("data URI has no comma separator")
    if not payload:
        raise RequestError("data URI is empty")

    meta = header[5:] if header.startswith("data:") else header
    mime = meta.split(";", 1)[0].strip().lower()
    if not mime:
        raise RequestError("data URI declares no media type")
    if not is_inline_media(mime):
        raise RequestError(
            "%s cannot be sent inline; Gemini accepts images, audio, video and PDF"
            % mime
        )
    if ";base64" not in meta:
        raise RequestError("data URI must be base64-encoded")

    # Base64 is 4/3 of the payload size.
    if len(payload) * 0.75 > MAX_INLINE_BYTES:
        raise RequestError(
            "attachment is larger than the %d MB Gemini accepts inline"
            % (MAX_INLINE_BYTES // (1024 * 1024))
        )
    return mime, payload


def content_to_parts(content):
    """Translate one OpenAI message body into Gemini parts."""
    if content is None:
        # Assistant turns that only contain tool calls have `content: null`.
        return []

    if isinstance(content, str):
        return [{"text": content}] if content else []

    if not isinstance(content, list):
        raise RequestError("message content must be a string or a list of parts")

    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise RequestError("message content parts must be objects")

        part_type = part.get("type")
        if part_type in ("text", "input_text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})

        elif part_type in ("image_url", "input_image"):
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str) or not url:
                raise RequestError("image_url part has no URL")
            if url.startswith("data:"):
                # A data URI carries its own media type, so a PDF or a video
                # rides the same path as a picture.
                mime, payload = parse_data_uri(url)
                parts.append({"inlineData": {"mimeType": mime, "data": payload}})
            elif url.startswith(("http://", "https://")):
                # Canvas cannot fetch arbitrary URLs, and fileData needs an
                # upload step the Canvas key does not permit, so the host
                # fetches and inlines the bytes itself. Restricted to images:
                # pulling a caller-supplied video is not worth the surface.
                try:
                    mime, payload = fetch_remote_image(url)
                    parts.append({"inlineData": {"mimeType": mime, "data": payload}})
                except ImageFetchError as exc:
                    log_dropped_image(url, str(exc))
                    parts.append({"text": "[Remote file omitted: %s]" % exc})
            else:
                raise RequestError("unsupported URL scheme")

        elif part_type == "input_audio":
            audio = part.get("input_audio") or {}
            payload = audio.get("data")
            if not payload:
                raise RequestError("input_audio part has no data")
            fmt = (audio.get("format") or "wav").lower()
            parts.append({"inlineData": {
                "mimeType": "audio/%s" % ("mpeg" if fmt in ("mp3", "mpeg") else fmt),
                "data": payload,
            }})

        elif part_type in ("file", "input_file"):
            # OpenAI-style file part: either a data URI or raw base64 plus a
            # mime type. Gemini takes it inline as long as the type is one it
            # accepts, which covers PDF and video.
            blob = part.get("file") or part.get("input_file") or {}
            if not isinstance(blob, dict):
                raise RequestError("file part is malformed")

            data = blob.get("file_data") or blob.get("data")
            if not data:
                raise RequestError(
                    "file parts must carry base64 file_data; file ids are not "
                    "supported because the Canvas key cannot fetch them"
                )
            if data.startswith("data:"):
                mime, payload = parse_data_uri(data)
            else:
                mime = (blob.get("mime_type") or blob.get("mimeType") or "").lower()
                if not mime:
                    raise RequestError("file part needs a mime_type")
                if not is_inline_media(mime):
                    raise RequestError(
                        "%s cannot be sent inline; Gemini accepts images, audio, "
                        "video and PDF" % mime
                    )
                payload = data
            parts.append({"inlineData": {"mimeType": mime, "data": payload}})

        # Unknown part types are skipped: clients add fields like
        # `cache_control` that have no Gemini equivalent, and failing the whole
        # request over one is worse than ignoring it.

    return parts


def sanitize_schema(schema):
    """Strip JSON Schema keywords Gemini rejects, recursively."""
    if isinstance(schema, dict):
        cleaned = {}
        for key, value in schema.items():
            if key in UNSUPPORTED_SCHEMA_KEYS:
                continue
            # Gemini rejects a boolean `items`; true means "any", which maps to {}.
            if key == "items" and isinstance(value, bool):
                if value:
                    cleaned[key] = {}
                continue
            cleaned[key] = sanitize_schema(value)
        return cleaned
    if isinstance(schema, list):
        return [sanitize_schema(item) for item in schema]
    return schema


def uppercase_types(schema):
    """Gemini wants SCREAMING_CASE type values."""
    if isinstance(schema, dict):
        if isinstance(schema.get("type"), str) and not schema["type"].isupper():
            schema["type"] = schema["type"].upper()
        for value in schema.values():
            uppercase_types(value)
    elif isinstance(schema, list):
        for item in schema:
            uppercase_types(item)
    return schema


def translate_tools(tools):
    """Convert OpenAI tool definitions into one Gemini tools object."""
    declarations = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        declarations.append({
            "name": name,
            "description": function.get("description", ""),
            "parameters": uppercase_types(sanitize_schema(parameters)),
        })
    return [{"functionDeclarations": declarations}] if declarations else None


def translate_response_format(response_format):
    """Map OpenAI response_format onto Gemini's generationConfig fields."""
    if not isinstance(response_format, dict):
        return {}

    kind = response_format.get("type")
    if kind == "json_object":
        return {"responseMimeType": "application/json"}
    if kind == "json_schema":
        spec = response_format.get("json_schema") or {}
        schema = spec.get("schema")
        if isinstance(schema, dict):
            return {
                "responseMimeType": "application/json",
                "responseSchema": uppercase_types(sanitize_schema(schema)),
            }
    return {}


TTS_MODEL_PATTERN = re.compile(r"-tts$")
DEFAULT_VOICE = "Kore"


def openai_to_gemini(body, model=""):
    """Convert an OpenAI chat completion request into Gemini generateContent."""
    if not isinstance(body, dict):
        raise RequestError("request body must be a JSON object")

    messages = body.get("messages")
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        raise RequestError("messages must be a list")

    system_texts = []
    contents = []
    pending_tool_parts = []
    tool_result_limit = env_int(
        "PROXY_TOOL_RESULT_MAX_CHARS", DEFAULT_TOOL_RESULT_CHARS, minimum=1
    )

    def flush_tool_parts():
        if pending_tool_parts:
            contents.append({"role": "user", "parts": list(pending_tool_parts)})
            pending_tool_parts.clear()

    # First pass: map tool_call ids to names so a result does not need to
    # search the history for every message.
    tool_names = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                tool_names[call["id"]] = (call.get("function") or {}).get("name", "")

    for message in messages:
        if not isinstance(message, dict):
            raise RequestError("each message must be an object")

        role = message.get("role", "user")
        content = message.get("content")

        if role != "tool":
            flush_tool_parts()

        if role == "system" or role == "developer":
            # OpenAI permits several system messages; Gemini has one system
            # instruction. Keep every one of them, in order.
            texts = [
                part["text"]
                for part in content_to_parts(content)
                if "text" in part
            ]
            system_texts.extend(texts)
            continue

        if role == "assistant" and message.get("tool_calls"):
            parts = content_to_parts(content)
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                raw_arguments = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except ValueError:
                    # A model that emitted invalid JSON still has to be
                    # replayable; send the raw string so Gemini can see it.
                    arguments = {"__raw_arguments": raw_arguments}
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}

                part = {
                    "functionCall": {
                        "name": function.get("name", ""),
                        "args": arguments,
                    }
                }
                signature = call.get("x_gemini_thought_signature")
                if not signature:
                    signature = "context_engineering_is_the_way_to_go"
                    warn(
                        "missing thoughtSignature for function call %r; using the "
                        "compatibility placeholder",
                        function.get("name", "<unnamed>"),
                    )
                part["thoughtSignature"] = signature
                parts.append(part)
            contents.append({"role": "model", "parts": parts})
            continue

        if role == "tool":
            text = content if isinstance(content, str) else json.dumps(content)
            if len(text) > tool_result_limit:
                lost = len(text) - tool_result_limit
                text = text[:tool_result_limit] + "\n... (%d characters truncated)" % lost
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"output": text}
            if not isinstance(payload, dict):
                # Gemini's functionResponse.response must be an object.
                payload = {"output": payload}
            pending_tool_parts.append({
                "functionResponse": {
                    "name": tool_names.get(message.get("tool_call_id"), "")
                    or message.get("name", ""),
                    "response": payload,
                }
            })
            continue

        parts = content_to_parts(content)
        if not parts:
            continue
        contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})

    flush_tool_parts()

    # Gemini rejects two consecutive turns with the same role.
    merged = []
    for turn in contents:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["parts"].extend(turn["parts"])
        else:
            merged.append(turn)

    result = {"contents": merged, "generationConfig": {}}
    if system_texts:
        result["systemInstruction"] = {"parts": [{"text": t} for t in system_texts]}

    config = result["generationConfig"]
    if "temperature" in body:
        config["temperature"] = body["temperature"]
    if body.get("max_completion_tokens") is not None:
        config["maxOutputTokens"] = body["max_completion_tokens"]
    elif body.get("max_tokens") is not None:
        config["maxOutputTokens"] = body["max_tokens"]
    if "top_p" in body:
        config["topP"] = body["top_p"]
    if "top_k" in body:
        config["topK"] = body["top_k"]
    if "seed" in body:
        config["seed"] = body["seed"]
    if "frequency_penalty" in body:
        config["frequencyPenalty"] = body["frequency_penalty"]
    if "presence_penalty" in body:
        config["presencePenalty"] = body["presence_penalty"]

    if "stop" in body:
        stop = body["stop"]
        if isinstance(stop, str):
            config["stopSequences"] = [stop]
        elif isinstance(stop, list) and all(isinstance(item, str) for item in stop):
            config["stopSequences"] = stop
        elif stop is not None:
            warn("ignoring `stop`: expected a string or a list of strings")

    config.update(translate_response_format(body.get("response_format")))

    # Speech models are a different modality, not a different prompt. They need
    # AUDIO in the response modalities and a voice; without both, Gemini answers
    # with text and the caller wonders why there is no audio.
    if TTS_MODEL_PATTERN.search(model or ""):
        config["responseModalities"] = ["AUDIO"]
        voice = (
            body.get("voice")
            or (body.get("extra_body") or {}).get("google", {}).get("voice")
            or DEFAULT_VOICE
        )
        config["speechConfig"] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
        }

    # Callers that know Gemini can pass generationConfig fields straight
    # through (thinkingConfig, safetySettings-adjacent knobs, etc.) without
    # this proxy inventing a mapping for each one.
    extra_config = body.get("extra_body", {}).get("google", {}).get("generation_config") \
        if isinstance(body.get("extra_body"), dict) else None
    if isinstance(extra_config, dict):
        config.update(extra_config)

    tools = translate_tools(body.get("tools") or [])
    if tools:
        result["tools"] = tools

    if body.get("n", 1) not in (1, None):
        warn("ignoring `n`: Gemini returns one candidate per request")
    if body.get("logprobs"):
        warn("ignoring `logprobs`: not exposed by the Canvas key")
    if body.get("top_logprobs") is not None:
        warn("ignoring `top_logprobs`: not exposed by the Canvas key")

    return result


# ───────────────────────────────────────────────────────────────────────────
# Response translation: Gemini -> OpenAI
# ───────────────────────────────────────────────────────────────────────────

class GeminiResponseError(ValueError):
    """Gemini returned no usable completion."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def map_finish_reason(reason):
    normalized = str(reason or "STOP").upper()
    mapped = FINISH_REASON_MAP.get(normalized)
    if mapped is None:
        warn("unmapped Gemini finishReason %s; reporting `stop`", normalized)
        return "stop"
    return mapped


def extract_usage(gemini_response):
    meta = (gemini_response or {}).get("usageMetadata") or {}
    prompt = meta.get("promptTokenCount", 0)
    completion = meta.get("candidatesTokenCount", 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": meta.get("totalTokenCount", prompt + completion),
    }


def gemini_to_openai(gemini_response, model):
    """Convert a Gemini generateContent response into an OpenAI completion."""
    candidates = (gemini_response or {}).get("candidates") or []
    if not candidates:
        feedback = (gemini_response or {}).get("promptFeedback") or {}
        raise GeminiResponseError(
            "Gemini returned no candidates (block reason: %s)"
            % feedback.get("blockReason", "UNKNOWN"),
            {
                "blockReason": feedback.get("blockReason", "UNKNOWN"),
                "safetyRatings": feedback.get("safetyRatings", []),
            },
        )

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    raw_finish = str(candidate.get("finishReason", "STOP")).upper()

    if not parts and raw_finish != "STOP":
        raise GeminiResponseError(
            "Gemini returned no content (finish reason: %s)" % raw_finish,
            {
                "finishReason": raw_finish,
                "safetyRatings": candidate.get("safetyRatings", []),
            },
        )

    text_segments = []
    image_segments = []
    audio_parts = []
    tool_calls = []

    for part in parts:
        if "text" in part and part["text"]:
            text_segments.append(part["text"])
        elif "inlineData" in part:
            data = part["inlineData"].get("data", "")
            mime = part["inlineData"].get("mimeType", "image/png")
            if is_audio(mime):
                audio_parts.append((mime, data))
            else:
                image_segments.append("![generated_image](data:%s;base64,%s)" % (mime, data))
        elif "functionCall" in part:
            call = part["functionCall"]
            tool_call = {
                "id": "call_%s" % uuid.uuid4().hex[:8],
                "type": "function",
                "function": {
                    "name": call.get("name", ""),
                    "arguments": json.dumps(call.get("args") or {}),
                },
            }
            if part.get("thoughtSignature"):
                tool_call["x_gemini_thought_signature"] = part["thoughtSignature"]
            tool_calls.append(tool_call)

    content = "\n".join(text_segments + image_segments)
    completion = {
        "id": "chatcmpl-%s" % uuid.uuid4().hex[:8],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "tool_calls" if tool_calls else map_finish_reason(raw_finish),
        }],
        "usage": extract_usage(gemini_response),
    }
    if tool_calls:
        completion["choices"][0]["message"]["tool_calls"] = tool_calls

    if audio_parts:
        # Speech comes back as headerless PCM. Convert the first part to WAV and
        # expose it where an OpenAI client looks for audio, rather than hiding
        # it in the text content.
        import base64 as _b64
        mime, data = audio_parts[0]
        try:
            raw = _b64.b64decode(data)
            wav = pcm_to_wav(raw, sample_rate=audio_sample_rate(mime))
            encoded = _b64.b64encode(wav).decode("ascii")
        except Exception:  # noqa: BLE001 - a decode failure must not lose the text
            encoded = data
        completion["choices"][0]["message"]["audio"] = {
            "data": encoded,
            "mime_type": "audio/wav",
            "format": "wav",
        }
        completion["choices"][0]["message"]["content"] = content or ""

    return completion


# ───────────────────────────────────────────────────────────────────────────
# HTTP server
# ───────────────────────────────────────────────────────────────────────────

MODEL_CATALOG = [
    ("gemini-3-flash-preview", "Gemini 3 Flash — fast, strong at tool use"),
    ("gemini-2.5-flash-preview-05-20", "Gemini 2.5 Flash preview"),
    ("gemini-3.1-flash-image-preview", "Nano Banana 2 — image generation"),
    ("gemini-2.5-flash-image", "Nano Banana — image generation"),
    ("gemini-2.5-flash-preview-tts", "Gemini 2.5 Flash TTS — speech generation"),
    ("gemini-2.5-pro-preview-tts", "Gemini 2.5 Pro TTS — higher quality speech"),
]


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class APIHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible HTTP surface.

    Every externally visible behaviour here is covered by
    05-runs/harness/http_probe.sh; change one and the probe must change too.
    """

    protocol_version = "HTTP/1.1"
    server_version = "gemini-canvas-proxy"
    sys_version = ""

    # One shared start time so /status can report uptime.
    started_at = time.time()

    # ── routing ────────────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {
                "status": "ok",
                "bridge": self._bridge_snapshot(),
                "uptime_seconds": round(time.time() - self.started_at, 1),
            })
            return
        if path == "/status":
            if not self._authorized():
                return
            self._json(200, self._status_payload())
            return
        if path == "/v1/models":
            if not self._authorized():
                return
            self._json(200, {"object": "list", "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "google",
                 "description": description}
                for model, description in MODEL_CATALOG
            ]})
            return
        if path == "/diagnostics":
            if not self._authorized():
                return
            self._handle_diagnostics()
            return
        if path.startswith("/v1/models/"):
            if not self._authorized():
                return
            model = path[len("/v1/models/"):]
            entry = dict(MODEL_CATALOG).get(model)
            if entry is None:
                self._error(404, "model %r is not in the advertised list" % model)
                return
            self._json(200, {"id": model, "object": "model", "created": 0,
                             "owned_by": "google", "description": entry})
            return
        self._error(404, "unknown endpoint: %s" % path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            self._error(404, "unknown endpoint: %s" % path)
            return
        if not self._authorized():
            return
        self._handle_chat_completions()

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        allowed = os.environ.get("PROXY_ALLOWED_ORIGIN", "").strip()
        if origin and (not allowed or origin != allowed):
            self._error(403, "origin is not allowed")
            return
        self.send_response(204)
        if allowed:
            self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── handlers ───────────────────────────────────────────────────────────

    def _handle_diagnostics(self):
        """Ask the Canvas page what it can see, and return its answer.

        The page is the only component the host cannot inspect directly. Without
        this, diagnosing "the key never arrived" means asking someone to open a
        devtools console inside a sandboxed iframe.
        """
        extension = self._ask_extension()

        request_id = str(uuid.uuid4())
        queue = register_request(request_id)
        try:
            outbound_sender({
                "type": "api_request",
                "id": request_id,
                "method": "GET",
                "path": "/diag",
                "body": None,
                "headers": {},
                "stream": False,
            })
            item = queue.get(timeout=10)
        except Empty:
            self._error(504, "the Canvas page did not answer the diagnostics request")
            return
        except (ConnectionError, OSError) as exc:
            self._error(502, "bridge unavailable: %s" % exc)
            return
        finally:
            unregister_request(request_id)

        if item.get("error"):
            self._error(502, item["error"])
            return
        self._json(200, {
            "object": "proxy.diagnostics",
            "extension": extension,
            "page": item.get("data") or {},
            "bridge": self._bridge_snapshot(),
        })

    def _ask_extension(self):
        """Ask the service worker which tab it would route to, and whether that
        tab's preview is mounted. Returns {} if the extension does not answer."""
        request_id = str(uuid.uuid4())
        queue = register_request(request_id)
        try:
            outbound_sender({"type": "status_request", "id": request_id})
            item = queue.get(timeout=5)
            return item.get("data") or {}
        except (Empty, ConnectionError, OSError):
            return {}
        finally:
            unregister_request(request_id)

    def _handle_chat_completions(self):
        started = time.monotonic()

        try:
            body = self._read_json_body()
            model = body.get("model") or "gemini-3-flash-preview"
            if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model) or ".." in model:
                raise RequestError(
                    "invalid model identifier; use only letters, digits, '.', '_' and '-'"
                )
            gemini_body = openai_to_gemini(body, model)
        except RequestError as exc:
            self._error(exc.status, str(exc))
            return
        except ConfigError as exc:
            self._error(500, "proxy misconfigured: %s" % exc)
            return

        streaming = bool(body.get("stream"))
        if streaming and TTS_MODEL_PATTERN.search(model):
            # A WAV needs its header before its body, so there is nothing
            # incremental to send. Refusing beats returning an SSE stream whose
            # only payload is unusable, or silently switching the caller to a
            # shape it did not ask for.
            self._error(400, "speech models do not support streaming; "
                             "send the request without stream")
            return
        request_id = str(uuid.uuid4())
        queue = register_request(request_id)

        message = {
            "type": "api_request",
            "id": request_id,
            "method": "POST",
            "path": "/v1beta/models/%s:%s" % (
                model, "streamGenerateContent" if streaming else "generateContent"
            ),
            "body": gemini_body,
            "headers": {},
            "stream": streaming,
            "stream_options": body.get("stream_options") or {},
        }

        try:
            self._dispatch(request_id, message)
        except (ConnectionError, OSError) as exc:
            unregister_request(request_id)
            self._error(502, "bridge unavailable: %s" % exc)
            return
        except ValueError as exc:
            unregister_request(request_id)
            self._error(413, str(exc))
            return

        try:
            if streaming:
                self._relay_stream(request_id, queue, model, body, started)
            else:
                self._relay_buffered(request_id, queue, model, started)
        finally:
            unregister_request(request_id)

    def _dispatch(self, request_id, message):
        """Send a request to the bridge, chunking if it exceeds the frame limit."""
        serialized = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(serialized) <= MAX_HOST_TO_BROWSER_BYTES * 0.9:
            outbound_sender(message)
            return

        chunks = chunk_request(serialized, request_id)
        log("request %s is %d bytes; sending in %d chunks",
            request_id[:8], len(serialized), len(chunks))
        for chunk in chunks:
            outbound_sender(chunk)

    def _relay_buffered(self, request_id, queue, model, started):
        timeout = self._request_timeout(model)
        try:
            item = queue.get(timeout=timeout)
        except Empty:
            self._error(504, self._timeout_message(request_id, timeout))
            return
        except ConfigError as exc:
            self._error(500, "proxy misconfigured: %s" % exc)
            return

        if item["kind"] != "response":
            self._error(502, "bridge sent a streaming event for a non-streaming request")
            return

        if item.get("error"):
            self._error(502, item["error"])
            return

        try:
            completion = gemini_to_openai(item.get("data") or {}, model)
        except GeminiResponseError as exc:
            self._error(502, str(exc), exc.details)
            return

        self._json(200, completion)
        self._log_access(200, model, started, "buffered")

    def _relay_stream(self, request_id, queue, model, body, started):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        idle_timeout = env_float(
            "PROXY_STREAM_IDLE_TIMEOUT", self._request_timeout(model), minimum=1
        )
        completion_id = "chatcmpl-%s" % uuid.uuid4().hex[:8]
        created = int(time.time())

        def chunk(delta, finish_reason=None):
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            if not delta and finish_reason is None:
                payload["choices"] = []
            return payload

        headers_sent = False
        usage = None
        finish_reason = "stop"
        tool_index = 0

        def start_stream():
            nonlocal headers_sent
            if headers_sent:
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self._cors_headers()
            self.end_headers()
            self.close_connection = True
            headers_sent = True

        def emit(payload):
            self.wfile.write(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        try:
            while True:
                try:
                    item = queue.get(timeout=idle_timeout)
                except Empty:
                    if not headers_sent:
                        self._error(504, self._timeout_message(request_id, idle_timeout))
                    else:
                        emit(chunk({}, "stop"))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    return
                except ConfigError as exc:
                    self._error(500, "proxy misconfigured: %s" % exc)
                    return

                if item["kind"] != "stream":
                    if not headers_sent:
                        self._error(502, "bridge sent a buffered response to a streaming request")
                    return

                event = item.get("event")

                if event == "error":
                    if not headers_sent:
                        self._error(502, item.get("error") or "stream failed")
                    else:
                        emit(chunk({"content": "\n[proxy error: %s]" % item.get("error")}))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    return

                if event == "start":
                    start_stream()
                    emit(chunk({"role": "assistant", "content": ""}))
                    continue

                start_stream()

                if event == "delta":
                    if item.get("text"):
                        emit(chunk({"content": item["text"]}))
                    continue

                if event == "tool_call":
                    call = item.get("tool_call") or {}
                    emit(chunk({"tool_calls": [{
                        "index": call.get("index", tool_index),
                        "id": call.get("id") or "call_%s" % uuid.uuid4().hex[:8],
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": call.get("arguments", "{}"),
                        },
                    }]}))
                    tool_index = call.get("index", tool_index) + 1
                    continue

                if event == "end":
                    finish_reason = item.get("finish_reason") or "stop"
                    usage = item.get("usage")
                    break

            if usage and include_usage:
                final = chunk({}, finish_reason)
                final["choices"] = []
                final["usage"] = usage
                emit(final)
            else:
                emit(chunk({}, finish_reason))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self._log_access(200, model, started, "streamed")

        except (BrokenPipeError, ConnectionResetError):
            # The client hung up mid-stream. Nothing to report, nothing to fix.
            log("client disconnected during stream %s", request_id[:8])

    # ── helpers ────────────────────────────────────────────────────────────

    def _bridge_snapshot(self):
        if transport == "native":
            # stdio is the bridge. This process exists because Chrome started it
            # and is holding the other end of the pipes.
            return {"transport": "native", "connected": True, "connected_seconds": 0}
        with bridge_lock:
            connected = bridge_connection is not None
            since = bridge_connected_at
        return {
            "transport": "socket",
            "connected": connected,
            "connected_seconds": round(time.monotonic() - since, 1) if since else 0,
        }

    def _status_payload(self):
        with pending_lock:
            inflight = len(pending_requests)
        return {
            "object": "proxy.status",
            "protocol": PROTOCOL_VERSION,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "bridge": self._bridge_snapshot(),
            "inflight_requests": inflight,
            "config": {
                "bind": os.environ.get("PROXY_BIND", "127.0.0.1"),
                "port": env_int("PROXY_PORT", DEFAULT_PORT),
                "request_timeout_seconds": env_float(
                    "PROXY_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT
                ),
                "max_request_bytes": env_int(
                    "PROXY_MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES
                ),
                "url_fetch_enabled": env_flag("PROXY_ALLOW_URL_FETCH"),
            },
        }

    @staticmethod
    def _model_produces_media(model):
        """True when the model returns pixels or audio instead of text."""
        name = (model or "").lower()
        return any(hint in name for hint in MEDIA_MODEL_HINTS)

    @classmethod
    def _request_timeout(cls, model):
        """Ceiling for one request: PROXY_REQUEST_TIMEOUT, raised to
        PROXY_MEDIA_TIMEOUT for models that produce media."""
        timeout = env_float("PROXY_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT, minimum=1)
        if cls._model_produces_media(model):
            media = env_float("PROXY_MEDIA_TIMEOUT", DEFAULT_MEDIA_TIMEOUT, minimum=1)
            timeout = max(timeout, media)
        return timeout

    def _timeout_message(self, request_id, timeout):
        snapshot = self._bridge_snapshot()
        if not snapshot["connected"]:
            return ("no request reached the browser: the extension is not connected. "
                    "Load the extension and open the Canvas page.")
        return ("the Canvas page did not answer within %gs (request %s). "
                "Check that the proxy HTML is open in Canvas Preview."
                % (timeout, request_id[:8]))

    def _read_json_body(self):
        max_bytes = env_int(
            "PROXY_MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES, minimum=1
        )
        # Chunking is the transport for large bodies, so the ceiling is
        # whichever limit bites first.
        limit = min(max_bytes, MAX_CHUNKED_REQUEST_BYTES)

        raw = self._read_body(limit)
        if not raw:
            raise RequestError("request body is empty")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise RequestError("request body is not valid JSON: %s" % exc) from exc
        if not isinstance(body, dict):
            raise RequestError("request body must be a JSON object")
        return body

    def _read_body(self, limit):
        declared = self.headers.get("Content-Length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as exc:
                raise RequestError("Content-Length is not a number") from exc
            if length < 0:
                raise RequestError("Content-Length is negative")
            if length > limit:
                raise RequestError(
                    "request body of %d bytes exceeds the %d-byte limit" % (length, limit),
                    status=413,
                )
            return self.rfile.read(length)

        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            return self._read_chunked_body(limit)

        raise RequestError("request has no Content-Length and is not chunked", status=411)

    def _read_chunked_body(self, limit):
        body = bytearray()
        while True:
            line = self.rfile.readline(1024).strip()
            if not line:
                break
            try:
                size = int(line.split(b";", 1)[0], 16)
            except ValueError as exc:
                raise RequestError("malformed chunked encoding") from exc
            if size == 0:
                self.rfile.readline(1024)  # trailing CRLF
                break
            if len(body) + size > limit:
                raise RequestError(
                    "request body exceeds the %d-byte limit" % limit, status=413
                )
            body.extend(self.rfile.read(size))
            self.rfile.readline(1024)  # CRLF after each chunk
        return bytes(body)

    def _authorized(self):
        expected = load_proxy_token()
        if not expected:
            self._error(503, "no bearer token configured; run the setup script")
            return False

        header = self.headers.get("Authorization", "")
        scheme, separator, supplied = header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.strip(), expected
        ):
            self._error(401, "missing or invalid bearer token", authenticate=True)
            return False
        return True

    def _cors_headers(self):
        allowed = os.environ.get("PROXY_ALLOWED_ORIGIN", "").strip()
        origin = self.headers.get("Origin")
        if allowed and origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message, details=None, authenticate=False):
        error = {"message": message, "type": "proxy_error", "code": status}
        if details:
            error["details"] = details
        body = json.dumps({"error": error}).encode("utf-8")

        # The request body may not have been read (413, 411, bad auth). If the
        # connection stayed alive, the unread bytes would be parsed as the next
        # request line — so every error closes the connection.
        self.close_connection = True

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self._cors_headers()
        if authenticate:
            self.send_header("WWW-Authenticate", 'Bearer realm="gemini-canvas-proxy"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log_access(self, status, model, started, mode):
        log("%d %s %s %.2fs", status, model, mode, time.monotonic() - started)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Silence the stdlib access log; _log_access writes the useful line."""

    def log_error(self, format, *args):  # noqa: A002 - stdlib signature
        log("http error: " + (format % args if args else format))


# ───────────────────────────────────────────────────────────────────────────
# Entry points
# ───────────────────────────────────────────────────────────────────────────

def run_stdio_bridge(socket_path, port):
    """Native-messaging side of the split Docker deployment."""
    connection = connect_bridge_socket(socket_path)
    log("bridge-only mode connected to %s", socket_path)

    def socket_to_stdio():
        try:
            while True:
                message = read_socket_message(connection)
                if message is None:
                    break
                send_message(message)
        except Exception as exc:  # noqa: BLE001 - the pump ends, main() cleans up
            warn("socket -> stdio pump stopped: %s", exc)
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=socket_to_stdio, daemon=True).start()
    send_message({"type": "host_ready", "port": port, "protocol": PROTOCOL_VERSION})

    while True:
        message = read_message()
        if message is None:
            break
        write_socket_message(connection, message)
    connection.close()


def main():
    global outbound_sender
    started_monotonic = time.monotonic()

    parser = argparse.ArgumentParser(
        description="OpenAI-compatible HTTP API backed by Gemini Canvas.",
        allow_abbrev=False,
    )
    role = parser.add_mutually_exclusive_group()
    role.add_argument("--http-only", action="store_true",
                      help="serve HTTP and accept one bridge on a Unix socket")
    role.add_argument("--bridge-only", action="store_true",
                      help="pump native messaging stdio to the Unix socket")
    # Chrome invokes a native messaging host as `path chrome-extension://<id>/`
    # — the calling origin arrives as a positional argument. parse_args() would
    # reject it as unrecognised and exit 2, which Chrome reports as
    # "Native host has exited", once per service-worker wake, forever.
    args, passthrough = parser.parse_known_args()
    caller_origin = passthrough[0] if passthrough else ""

    try:
        port = env_int("PROXY_PORT", DEFAULT_PORT, minimum=1, maximum=65535)
        bind = os.environ.get("PROXY_BIND", "127.0.0.1").strip() or "127.0.0.1"
        socket_path = os.environ.get("PROXY_SOCKET", "/tmp/gemini-canvas-proxy.sock")
    except ConfigError as exc:
        log("configuration error: %s", exc)
        return 2

    if args.bridge_only:
        try:
            run_stdio_bridge(socket_path, port)
        except Exception as exc:  # noqa: BLE001 - report and exit non-zero for Chrome
            log("bridge-only mode failed: %s", exc)
            return 1
        return 0

    try:
        server = ThreadedHTTPServer((bind, port), APIHandler)
    except OSError as exc:
        raise SystemExit(
            "cannot listen on %s:%d (%s) — another process is probably already "
            "serving the proxy" % (bind, port, exc.strerror or exc)
        )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("HTTP listening on http://%s:%d (pid %d)", bind, port, os.getpid())
    record_fatal("started on %s:%d in %s role, caller=%s" % (
        bind, port,
        "http-only" if args.http_only else "bridge-only" if args.bridge_only else "native",
        caller_origin or "<none>"))
    if bind not in ("127.0.0.1", "localhost", "::1"):
        warn("listening on %s; the bearer token is the only access control", bind)

    if args.http_only:
        global transport
        transport = "socket"
        outbound_sender = send_to_bridge
        try:
            serve_bridge_socket(socket_path)
        except Exception as exc:  # noqa: BLE001 - nothing useful happens without it
            log("bridge socket failed: %s", exc)
            return 1
        return 0

    # Bare metal: Chrome launched us, so stdio is the bridge.
    try:
        send_message({
            "type": "host_ready",
            "port": port,
            "protocol": PROTOCOL_VERSION,
        })
    except (BrokenPipeError, OSError):
        log("Chrome closed the native port at startup; HTTP stays up until it returns")

    while True:
        try:
            message = read_message()
        except ValueError as exc:
            warn("dropping malformed native message: %s", exc)
            continue
        except Exception as exc:  # noqa: BLE001 - stdin failure ends the loop
            log("stdin failed: %s", exc)
            break
        if message is None:
            break
        route_bridge_message(message)

    # Reaching here means stdin closed. That is either a clean browser shutdown
    # or the extension dropping the port mid-flight; the log distinguishes it
    # from a crash, which would have produced a traceback instead.
    log("native port closed; shutting down")
    record_fatal("native port closed after %.1fs — Chrome ended the session" % (
        time.monotonic() - started_monotonic))
    server.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as exc:
        if exc.code not in (0, None):
            record_fatal("exited with %s  argv=%r  env_port=%r" % (
                exc.code, sys.argv[1:], os.environ.get("PROXY_PORT")))
        raise
    except BaseException:
        import traceback
        record_fatal("unhandled exception\n" + traceback.format_exc())
        raise
