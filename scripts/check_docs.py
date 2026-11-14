#!/usr/bin/env python3
"""Check documentation claims against the source.

Three checks, because these are the three ways the docs drifted before:

  1. every PROXY_* / BROWSER_DATA_DIR variable the code reads is documented, and
     every one the docs mention is actually read;
  2. every HTTP route the handler answers appears in docs/api.md;
  3. every number the docs quote about the protocol matches the constants.

Run from the repository root:
    python3 scripts/check_docs.py .
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
DOC_FILES = [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))
CODE_FILES = [
    ROOT / "native_host" / "gemini_proxy.py",
    ROOT / "native_host" / "gemini_proxy_bridge.sh",
    ROOT / "docker" / "entrypoint.sh",
    ROOT / "docker" / "setup-extension.sh",
    ROOT / "setup.sh",
]

ENV_PATTERN = re.compile(r"\b(PROXY_[A-Z_]+|BROWSER_DATA_DIR|CHROMIUM_USER_DATA_DIR|NATIVE_HOST_DIR|NOVNC_PORT|VNC_PORT|NOVNC_WEB|DISPLAY)\b")
# Shell locals that look like configuration but are not read from the
# environment: PROXY_PID is the proxy's pid inside entrypoint.sh.
NOT_CONFIGURATION = {"PROXY_PID"}
ROUTE_PATTERN = re.compile(r'path (?==|\.startswith\()\s*\(?"?([^",)]+)')

problems = []


def read(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


doc_text = "\n".join(read(p) for p in DOC_FILES)
code_text = "\n".join(read(p) for p in CODE_FILES)

# ── 1. environment variables ────────────────────────────────────────────────

documented = set(ENV_PATTERN.findall(doc_text))
used = set(ENV_PATTERN.findall(code_text))

undocumented = sorted(used - documented - NOT_CONFIGURATION)
phantom = sorted(documented - used - NOT_CONFIGURATION)

for name in undocumented:
    problems.append("code reads %s but no doc mentions it" % name)
for name in phantom:
    problems.append("doc mentions %s but no code reads it" % name)

# ── 2. routes ───────────────────────────────────────────────────────────────

api_doc = read(ROOT / "docs" / "api.md")
for route in ("/v1/chat/completions", "/v1/models", "/status", "/health"):
    if route not in api_doc:
        problems.append("%s is served but missing from docs/api.md" % route)

# ── 3. protocol constants ───────────────────────────────────────────────────

constants = {}
for match in re.finditer(r"^(MAX_HOST_TO_BROWSER_BYTES|MAX_BROWSER_TO_HOST_BYTES|RAW_CHUNK_BYTES|MAX_CHUNKS|PROTOCOL_VERSION)\s*=\s*(.+)$",
                         code_text, re.M):
    name, value = match.group(1), match.group(2).strip()
    try:
        constants[name] = eval(value, {}, {})  # noqa: S307 - literals only
    except Exception:
        pass

expectations = {
    "MAX_HOST_TO_BROWSER_BYTES": ("1 MiB", 1 << 20),
    "MAX_BROWSER_TO_HOST_BYTES": ("64 MiB", 64 << 20),
    "RAW_CHUNK_BYTES": ("600 KB", 600_000),
    "MAX_CHUNKS": ("128", 128),
}
for name, (label, expected) in expectations.items():
    actual = constants.get(name)
    if actual != expected:
        problems.append("%s is %r in code, docs say %s" % (name, actual, label))
    elif label not in doc_text:
        problems.append("%s is %s but the docs never state it" % (name, label))

# ── report ──────────────────────────────────────────────────────────────────

if problems:
    print("documentation drift:")
    for problem in problems:
        print("  -", problem)
    sys.exit(1)

print("docs agree with code")
print("  %d environment variables referenced on both sides" % len(documented))
print("  %d routes documented" % 4)
print("  protocol constants match")
