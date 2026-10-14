#!/usr/bin/env python3
"""Fetch recent Sina domestic futures main-contract 1m bars and build 5m bars."""

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


def fetch_rows(source: dict[str, Any]) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"symbol": source["provider_symbol"], "type": source["source_interval_minutes"]})
    url = f"{source['endpoint']}?{query}"
    result = subprocess.run(
        ["curl", "--max-time", "30", "-fsSL", "--retry", "3", url],
        check=True, capture_output=True, text=True,
    )
    start, end = result.stdout.find("(["), result.stdout.rfind("])" )
    if start < 0 or end < start:
        raise RuntimeError("source returned no futures minute rows")
    rows = json.loads(result.stdout[start + 1:end + 1])
    if not rows:
        raise RuntimeError("source returned an empty futures minute dataset")
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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    source, output = config["source"], config["output"]
    db_path = resolve(config_path, output["sqlite_path"])
    if args.dry_run:
        print({"symbol": source["symbol"], "provider_symbol": source["provider_symbol"], "database": str(db_path)})
        return

    tz = ZoneInfo(source["timezone"])
    source_rows: list[tuple[Any, ...]] = []
    grouped: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
    for item in fetch_rows(source):
        end = datetime.strptime(item["d"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz) + timedelta(minutes=1)
        start = end - timedelta(minutes=1)
        row = (
            source["symbol"], item["d"], epoch_ms(start), epoch_ms(end),
            float(item["o"]), float(item["h"]), float(item["l"]), float(item["c"]),
            int(float(item["v"])), int(float(item["p"])) if item.get("p") else None, source["name"],
        )
        source_rows.append(row)
        grouped[row[2] // (5 * 60 * 1000)].append(row)
    source_rows.sort(key=lambda row: row[2])
    target_rows: list[tuple[Any, ...]] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: row[2])
        if len(rows) != 5 or any(rows[index][3] != rows[index + 1][2] for index in range(4)):
            continue
        start_ts, end_ts = rows[0][2], rows[-1][3]
        bar_time = datetime.fromtimestamp(end_ts / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")
        target_rows.append((
            source["symbol"], bar_time, start_ts, end_ts, rows[0][4],
            max(row[5] for row in rows), min(row[6] for row in rows), rows[-1][7],
            sum(row[8] for row in rows), rows[-1][9], f"{source['name']} aggregated_from_1m",
        ))
    target_rows.sort(key=lambda row: row[2])
    if not target_rows:
        raise RuntimeError("no complete 5m bars were produced")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        for table in (output["source_table"], output["target_table"]):
            con.execute(schema(table))
            con.execute(f"DELETE FROM {table} WHERE symbol=?", (source["symbol"],))
        placeholders = ",".join("?" for _ in range(11))
        con.executemany(f"INSERT INTO {output['source_table']} VALUES ({placeholders})", source_rows)
        con.executemany(f"INSERT INTO {output['target_table']} VALUES ({placeholders})", target_rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(
        f"DONE symbol={source['symbol']} bars_1m={len(source_rows)} bars_5m={len(target_rows)} "
        f"start={source_rows[0][1]} end={source_rows[-1][1]} database={db_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
