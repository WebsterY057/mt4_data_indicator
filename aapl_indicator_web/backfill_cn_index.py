#!/usr/bin/env python3
"""Fetch recent Eastmoney CN index 1m bars and aggregate complete 5m bars."""

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
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"config is not a mapping: {path}")
    return value


def resolve(config_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def fetch_rows(source: dict[str, Any]) -> list[str]:
    params = {
        "secid": source["provider_symbol"], "klt": 1, "fqt": 1,
        "lmt": source["max_source_rows"], "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    url = f"{source['endpoint']}?{urllib.parse.urlencode(params)}"
    result = subprocess.run(["curl", "--max-time", "30", "-fsSL", "--retry", "3", url], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    rows = payload.get("data", {}).get("klines", [])
    if not rows:
        raise RuntimeError("source returned no CN index minute rows")
    return rows


def schema(table: str) -> str:
    return f"""CREATE TABLE IF NOT EXISTS {table} (
        symbol TEXT NOT NULL, bar_time TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
        volume INTEGER NOT NULL, open_interest INTEGER, source TEXT NOT NULL,
        PRIMARY KEY(symbol, bar_time))"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    source, output = config["source"], config["output"]
    tz = ZoneInfo(source["timezone"])
    source_rows: list[tuple[Any, ...]] = []
    grouped: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
    for line in fetch_rows(source):
        timestamp, open_, close, high, low, volume, amount, _ = line.split(",")
        start = datetime.strptime(timestamp, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        end = start + timedelta(minutes=1)
        row = (source["symbol"], timestamp + ":00", epoch_ms(start), epoch_ms(end), float(open_), float(high), float(low), float(close), int(float(volume)), None, source["name"])
        source_rows.append(row)
        grouped[row[2] // 300000].append(row)
    source_rows.sort(key=lambda row: row[2])
    target_rows: list[tuple[Any, ...]] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: row[2])
        if len(rows) != 5 or any(rows[i][3] != rows[i + 1][2] for i in range(4)):
            continue
        end_ts = rows[-1][3]
        target_rows.append((source["symbol"], datetime.fromtimestamp(end_ts / 1000, tz).strftime("%Y-%m-%d %H:%M:%S"), rows[0][2], end_ts, rows[0][4], max(r[5] for r in rows), min(r[6] for r in rows), rows[-1][7], sum(r[8] for r in rows), None, f"{source['name']} aggregated_from_1m"))
    db_path = resolve(config_path, output["sqlite_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        for table in (output["source_table"], output["target_table"]):
            con.execute(schema(table)); con.execute(f"DELETE FROM {table} WHERE symbol=?", (source["symbol"],))
        marks = ",".join("?" for _ in range(11))
        con.executemany(f"INSERT INTO {output['source_table']} VALUES ({marks})", source_rows)
        con.executemany(f"INSERT INTO {output['target_table']} VALUES ({marks})", target_rows)
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()
    print(f"DONE symbol={source['symbol']} bars_1m={len(source_rows)} bars_5m={len(target_rows)} start={source_rows[0][1]} end={source_rows[-1][1]}")


if __name__ == "__main__":
    main()
