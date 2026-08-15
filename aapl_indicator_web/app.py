from __future__ import annotations

import base64
import json
import os
import pty
import select
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, jsonify, render_template, request


ROOT = Path(__file__).resolve().parent


def load_config() -> dict[str, Any]:
    with (ROOT / "config.yaml").open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("config.yaml is not a mapping")
    return config


CONFIG = load_config()
CONFIG["server"]["host"] = os.environ.get("AAPL_VIS_HOST", CONFIG["server"]["host"])
CONFIG["server"]["port"] = int(os.environ.get("AAPL_VIS_PORT", CONFIG["server"]["port"]))
app = Flask(__name__, template_folder=str(ROOT / "templates"))


def ssh_args(remote_command: str) -> list[str]:
    remote = CONFIG["remote"]
    return [
        "ssh",
        "-o", "PreferredAuthentications=publickey,password,keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        f"{remote['user']}@{remote['host']}",
        remote_command,
    ]


def run_ssh(remote_command: str) -> str:
    args = ssh_args(remote_command)
    password = os.environ.get(CONFIG["remote"]["password_env"], "")
    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp(args[0], args)
    output = ""
    buffer = ""
    return_code = 1
    try:
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096).decode(errors="replace")
                except OSError:
                    chunk = ""
                if chunk:
                    output += chunk
                    buffer += chunk
                    lowered = buffer.lower()
                    if "yes/no" in lowered:
                        os.write(master_fd, b"yes\n")
                        buffer = ""
                    elif "password:" in lowered:
                        if not password:
                            raise RuntimeError(
                                f"SSH requires a password; set {CONFIG['remote']['password_env']} before starting the service"
                            )
                        os.write(master_fd, (password + "\n").encode())
                        buffer = ""
            finished_pid, status = os.waitpid(pid, os.WNOHANG)
            if finished_pid == pid:
                if os.WIFEXITED(status):
                    return_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    return_code = 128 + os.WTERMSIG(status)
                break
        if return_code != 0:
            raise RuntimeError(f"remote query failed rc={return_code}: {output[-1200:].strip()}")
        return output.strip()
    finally:
        os.close(master_fd)


REMOTE_QUERY = r'''
import base64, json, sys
import duckdb

request = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
allowed = request["allowed_tables"]
interval = request["interval"]
if interval not in allowed:
    raise ValueError("interval is not allowed")
table = allowed[interval]
start = request["date_start"]
end = request["date_end"]
max_points = max(100, min(int(request["max_points"]), 20000))

con = duckdb.connect(request["db_path"], read_only=True)
try:
    total = con.execute(
        f"SELECT count(*) FROM {table} WHERE CAST(bar_start_utc AS DATE) BETWEEN ? AND ?",
        [start, end],
    ).fetchone()[0]
    stride = max(1, (int(total) + max_points - 1) // max_points)
    columns = ["start_ts", "end_ts", "open", "high", "low", "close", "volume", "trade_count"]
    rows = con.execute(
        f"""
        WITH selected AS (
          SELECT *, row_number() OVER (ORDER BY bar_start_utc) AS rn
          FROM {table}
          WHERE CAST(bar_start_utc AS DATE) BETWEEN ? AND ?
        )
        SELECT
          epoch_ms(bar_start_utc) AS start_ts,
          epoch_ms(bar_end_utc) AS end_ts,
          open, high, low, close, volume, trade_count
        FROM selected
        WHERE (rn - 1) % ? = 0 OR rn = ?
        ORDER BY bar_start_utc
        """,
        [start, end, stride, total],
    ).fetchall()
    payload = {
        "interval": interval,
        "date_start": start,
        "date_end": end,
        "total_rows": int(total),
        "stride": stride,
        "columns": columns,
        "rows": [dict(zip(columns, row)) for row in rows],
    }
    print(json.dumps(payload, default=str, ensure_ascii=False))
finally:
    con.close()
'''


def parse_json_output(output: str) -> dict[str, Any]:
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("remote query returned no JSON")
    payload = json.loads(output[start:end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError("remote query returned invalid JSON")
    return payload


@app.get("/")
def index():
    boards = {
        key: {field: value for field, value in board.items() if field not in {"sqlite_path", "tables"}}
        for key, board in CONFIG["boards"].items()
    }
    return render_template(
        "index.html",
        boards=boards,
        default_board=CONFIG["data"]["default_board"],
        default_interval=CONFIG["data"]["default_interval"],
        indicator_config=CONFIG["indicators"],
        backtest_config=CONFIG["backtest"],
    )


def local_sqlite_bars(board: dict[str, Any], interval: str, date_start: str, date_end: str) -> dict[str, Any]:
    table = board["tables"][interval]
    max_points = max(100, min(int(board["max_points"]), 50000))
    uri = f"file:{Path(board['sqlite_path']).expanduser()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        where = "symbol = ? AND date(window_start_ms_utc / 1000, 'unixepoch') BETWEEN ? AND ?"
        params = [board["symbol"], date_start, date_end]
        total = int(con.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0])
        stride = max(1, (total + max_points - 1) // max_points)
        rows = con.execute(
            f"""
            WITH selected AS (
              SELECT *, row_number() OVER (ORDER BY window_start_ms_utc) AS rn
              FROM {table} WHERE {where}
            )
            SELECT window_start_ms_utc, window_end_ms_utc,
                   mid_open, mid_high, mid_low, mid_close,
                   bid_volume_sum + ask_volume_sum, tick_count
            FROM selected
            WHERE (rn - 1) % ? = 0 OR rn = ?
            ORDER BY window_start_ms_utc
            """,
            [*params, stride, total],
        ).fetchall()
    finally:
        con.close()
    columns = ["start_ts", "end_ts", "open", "high", "low", "close", "volume", "trade_count"]
    return {"interval": interval, "date_start": date_start, "date_end": date_end,
            "total_rows": total, "stride": stride, "columns": columns,
            "rows": [dict(zip(columns, row)) for row in rows]}


def okx_sqlite_bars(board: dict[str, Any], interval: str, date_start: str, date_end: str) -> dict[str, Any]:
    table = board["tables"][interval]
    max_points = max(100, min(int(board["max_points"]), 50000))
    uri = f"file:{Path(board['sqlite_path']).expanduser()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        where = "inst_id = ? AND date(start_ts / 1000, 'unixepoch') BETWEEN ? AND ?"
        params = [board["symbol"], date_start, date_end]
        total = int(con.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0])
        stride = max(1, (total + max_points - 1) // max_points)
        rows = con.execute(
            f"""
            WITH selected AS (
              SELECT *, row_number() OVER (ORDER BY start_ts) AS rn
              FROM {table} WHERE {where}
            )
            SELECT start_ts, end_ts, open, high, low, close, volume, trade_count
            FROM selected WHERE (rn - 1) % ? = 0 OR rn = ? ORDER BY start_ts
            """, [*params, stride, total]
        ).fetchall()
    finally:
        con.close()
    columns = ["start_ts", "end_ts", "open", "high", "low", "close", "volume", "trade_count"]
    return {"interval": interval, "date_start": date_start, "date_end": date_end,
            "total_rows": total, "stride": stride, "columns": columns,
            "rows": [dict(zip(columns, row)) for row in rows]}


@app.get("/api/bars")
def bars():
    board_key = request.args.get("board", CONFIG["data"]["default_board"])
    board = CONFIG["boards"].get(board_key)
    if board is None:
        return jsonify({"ok": False, "error": "unknown board"}), 400
    interval = request.args.get("interval", CONFIG["data"]["default_interval"])
    date_start = request.args.get("date_start", board["date_start"])
    date_end = request.args.get("date_end", board["date_end"])
    if board["backend"] == "unconfigured":
        return jsonify({"ok": False, "error": "未找到已保存的 OKX 代币化美股行情；现有 OKX 实时库只包含 BTC-USDT"}), 503
    if interval not in board.get("tables", {}):
        return jsonify({"ok": False, "error": "interval must be 1m or 5m"}), 400
    if board["backend"] == "local_sqlite":
        try:
            return jsonify({"ok": True, **local_sqlite_bars(board, interval, date_start, date_end)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
    if board["backend"] == "okx_sqlite":
        try:
            return jsonify({"ok": True, **okx_sqlite_bars(board, interval, date_start, date_end)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
    request_payload = {
        "allowed_tables": board["tables"],
        "interval": interval,
        "date_start": date_start,
        "date_end": date_end,
        "max_points": board["max_points"],
        "db_path": CONFIG["remote"]["duckdb_path"],
    }
    encoded_request = base64.b64encode(
        json.dumps(request_payload, ensure_ascii=True).encode("utf-8")
    ).decode("ascii")
    encoded_script = base64.b64encode(REMOTE_QUERY.encode("utf-8")).decode("ascii")
    remote_code = (
        "import base64; "
        f"exec(compile(base64.b64decode({encoded_script!r}), '<aapl-indicator-query>', 'exec'))"
    )
    command = (
        f"{shlex.quote(CONFIG['remote']['python'])} -c {shlex.quote(remote_code)} "
        f"{shlex.quote(encoded_request)}"
    )
    try:
        payload = parse_json_output(run_ssh(command))
        return jsonify({"ok": True, **payload})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "aapl-indicator-web", "port": CONFIG["server"]["port"]})


if __name__ == "__main__":
    app.run(
        host=CONFIG["server"]["host"],
        port=int(CONFIG["server"]["port"]),
        debug=False,
    )
