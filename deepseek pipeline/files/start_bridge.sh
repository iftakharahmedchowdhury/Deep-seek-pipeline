#!/usr/bin/env bash
# Start the DeepSeek Local Bridge server.
#   ./start_bridge.sh /path/to/your/project        (or run it from inside the project folder)
# Optional env vars are documented at the top of server.py (BRIDGE_PORT, BRIDGE_WRITE_ALLOW, ...).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${1:-${BRIDGE_ROOT:-$PWD}}"
if [ ! -d "$ROOT" ]; then
  echo "Project folder not found: $ROOT" >&2
  exit 1
fi
cd "$HERE"
echo "Starting the bridge for: $ROOT"
echo "(if the extension panel says 'token rejected', click Token... and paste the token printed below)"
BRIDGE_ROOT="$ROOT" exec python3 server.py
