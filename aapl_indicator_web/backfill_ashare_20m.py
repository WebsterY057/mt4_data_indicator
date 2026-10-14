#!/usr/bin/env python3
"""Fetch A-share 5m bars and aggregate session-aligned 20m bars."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"config is not a mapping: {path}")
    return config


def resolve(config_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def fetch_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    source = config["source"]
    params = {
        "symbol": source["provider_symbol"],
        "scale": source["source_interval_minutes"],
        "ma": "no",
        "datalen": source["datalen"],
    }
    url = f"{source['endpoint']}?{urllib.parse.urlencode(params)}"
    result = subprocess.run(
        ["curl", "--max-time", "30", "-fsSL", "--retry", "3", url],
        check=True, capture_output=True, text=True,
    )
    start, end = result.stdout.find("(["), result.stdout.rfind("])")
    if start < 0 or end < start:
        raise RuntimeError("source returned no 5m JSONP rows")
    rows = json.loads(result.stdout[start + 1:end + 1])
    if not rows:
        raise RuntimeError("source returned an empty 5m dataset")
    return rows


def table_schema(table: str) -> str:
    return f"""CREATE TABLE IF NOT EXISTS {table} (
        symbol TEXT NOT NULL, bar_time TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
        open REAL NOT NULL, close REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
        volume INTEGER NOT NULL, amount REAL, adjustment TEXT NOT NULL, volume_unit TEXT NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY(symbol, bar_time))"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    source, output = config["source"], config["output"]
    db_path = resolve(config_path, output["sqlite_path"])
    print({"database": str(db_path), "symbol": source["symbol"], "source_interval": "5m", "target_interval": output["target_interval"], "datalen": source["datalen"]}, flush=True)
    if args.dry_run:
        return
    payload = fetch_rows(config)
    tz = ZoneInfo(source["timezone"])
    source_minutes = int(source["source_interval_minutes"])
    source_rows, grouped = [], defaultdict(list)
    sessions = [(datetime.strptime(x["start"], "%H:%M").time(), datetime.strptime(x["end"], "%H:%M").time()) for x in config["sessions"]]
    for item in payload:
        end = datetime.strptime(item["day"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        start = end - timedelta(minutes=source_minutes)
        row = (source["symbol"], item["day"], epoch_ms(start), epoch_ms(end), float(item["open"]), float(item["close"]), float(item["high"]), float(item["low"]), int(item["volume"]), float(item["amount"]) if item.get("amount") else None, source["adjustment"], source["volume_unit"], source["name"])
        source_rows.append(row)
        for session_start, session_end in sessions:
            base = datetime.combine(start.date(), session_start, tz)
            limit = datetime.combine(start.date(), session_end, tz)
            if base <= start < limit:
                bucket = int((start - base).total_seconds() // (int(output["target_interval_minutes"]) * 60))
                grouped[(start.date(), session_start, bucket)].append(row)
                break
    target_rows = []
    for _, rows in sorted(grouped.items()):
        rows.sort(key=lambda x: x[2])
        if len(rows) != int(output["target_interval_minutes"]) // source_minutes:
            continue
        start_ts, end_ts = rows[0][2], rows[-1][3]
        bar_time = datetime.fromtimestamp(end_ts / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")
        amounts = [x[9] for x in rows if x[9] is not None]
        target_rows.append((source["symbol"], bar_time, start_ts, end_ts, rows[0][4], rows[-1][5], max(x[6] for x in rows), min(x[7] for x in rows), sum(x[8] for x in rows), sum(amounts) if amounts else None, source["adjustment"], source["volume_unit"], f"{source['name']} aggregated_from_5m"))
    if not target_rows:
        raise RuntimeError("no complete session-aligned 20m bars were produced")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(table_schema(output["source_table"]))
        con.execute(table_schema(output["target_table"]))
        con.execute(f"DELETE FROM {output['source_table']} WHERE symbol=?", (source["symbol"],))
        con.execute(f"DELETE FROM {output['target_table']} WHERE symbol=?", (source["symbol"],))
        placeholders = ",".join("?" for _ in range(13))
        con.executemany(f"INSERT INTO {output['source_table']} VALUES ({placeholders})", source_rows)
        con.executemany(f"INSERT INTO {output['target_table']} VALUES ({placeholders})", target_rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(f"DONE source_5m={len(source_rows)} bars_20m={len(target_rows)} start={target_rows[0][1]} end={target_rows[-1][1]} database={db_path}", flush=True)


if __name__ == "__main__":
    main()
