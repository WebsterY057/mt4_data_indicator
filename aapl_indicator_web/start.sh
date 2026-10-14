#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${AAPL_VIS_PORT:-5011}"
HOST="${AAPL_VIS_HOST:-0.0.0.0}"
LOG_DIR="$ROOT_DIR/logs"
PID_FILE="$LOG_DIR/server_${PORT}.pid"
LOG_FILE="$LOG_DIR/server_${PORT}.log"
PYTHON_BIN="${AAPL_VIS_PYTHON:-/Users/yy/Documents/Codex/单因子回测架构/.venv_price_curves/bin/python}"

NEEDS_REMOTE="$($PYTHON_BIN - "$ROOT_DIR/config.yaml" <<'PY'
import sys
import yaml

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    config = yaml.safe_load(handle) or {}
boards = config.get("boards", {})
print("1" if any(board.get("backend") == "remote_duckdb" for board in boards.values()) else "0")
PY
)"

if [[ "$NEEDS_REMOTE" == "1" && -z "${AAPL_VIS_REMOTE_PASSWORD:-}" ]]; then
  printf "SSH password: "
  IFS= read -r -s AAPL_VIS_REMOTE_PASSWORD </dev/tty
  printf "\n"
  export AAPL_VIS_REMOTE_PASSWORD
fi

mkdir -p "$LOG_DIR"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "AAPL indicator service already running: pid=$(cat "$PID_FILE") port=$PORT"
  exit 0
fi

cd "$ROOT_DIR"
nohup env AAPL_VIS_HOST="$HOST" AAPL_VIS_PORT="$PORT" "$PYTHON_BIN" app.py >"$LOG_FILE" 2>&1 &
pid="$!"
echo "$pid" >"$PID_FILE"
echo "AAPL indicator service started: pid=$pid url=http://$HOST:$PORT log=$LOG_FILE"
