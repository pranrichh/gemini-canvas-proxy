#!/usr/bin/env sh
# Chrome launches this from the native-messaging manifest. Its only job is to
# hand stdio to gemini_proxy.py in bridge-only mode, where it pumps frames to
# the Unix socket owned by the container's --http-only process.
#
# Resolve the module relative to this script so the wrapper also works outside
# the container, where /app does not exist.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
exec "${PYTHON:-python3}" "$here/gemini_proxy.py" --bridge-only
