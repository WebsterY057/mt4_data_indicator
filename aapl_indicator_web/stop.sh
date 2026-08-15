#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${AAPL_VIS_PORT:-5011}"
PID_FILE="$ROOT_DIR/logs/server_${PORT}.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "AAPL indicator service is not running"
  exit 0
fi
pid="$(cat "$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
fi
rm -f "$PID_FILE"
echo "AAPL indicator service stopped: pid=$pid"
