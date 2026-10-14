#!/usr/bin/env python3
"""Copy normalized IBKR trades from DuckDB and build local SQLite bars."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import yaml


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"config is not a mapping: {path}")
    return config


def resolve(config_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def validate_filter(value: str) -> str:
    if value != "trigger_field = 'last_size'":
        raise ValueError("unsupported volume_event_filter")
    return value


def create_schema(con: sqlite3.Connection, cfg: dict[str, Any]) -> None:
    out = cfg["output"]
    raw = ident(out["raw_table"])
    con.execute(f"""
        CREATE TABLE {raw} (
            symbol TEXT NOT NULL,
            event_ts_ms INTEGER NOT NULL,
            trade_price REAL NOT NULL,
            trade_size REAL NOT NULL,
            trigger_field TEXT,
            partition_date TEXT NOT NULL
        )
    """)
    con.execute(f"CREATE INDEX {ident(out['raw_table'] + '_symbol_ts')} ON {raw}(symbol, event_ts_ms)")
    for table in out["bars_tables"].values():
        con.execute(f"""
            CREATE TABLE {ident(table)} (
                inst_id TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                source_event_count INTEGER NOT NULL,
                PRIMARY KEY(inst_id, start_ts)
            )
        """)


def copy_raw(source: duckdb.DuckDBPyConnection, target: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    src, out = cfg["input"], cfg["output"]
    query = f"""
        SELECT symbol, epoch_ms({ident(src['event_time_column'])}),
               {ident(src['price_column'])}::DOUBLE,
               {ident(src['size_column'])}::DOUBLE,
               trigger_field, CAST(_partition_date AS VARCHAR)
        FROM {ident(src['schema'])}.{ident(src['table'])}
        WHERE symbol = ?
          AND _partition_date BETWEEN ?::DATE AND ?::DATE
          AND event = 'l1_trade'
          AND {ident(src['event_time_column'])} IS NOT NULL
          AND {ident(src['price_column'])} > 0
          AND {ident(src['size_column'])} >= 0
        ORDER BY {ident(src['event_time_column'])}
    """
    cursor = source.execute(query, [src["symbol"], src["date_start"], src["date_end"]])
    insert = f"INSERT INTO {ident(out['raw_table'])} VALUES (?, ?, ?, ?, ?, ?)"
    total, batch_size = 0, int(out["batch_size"])
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        target.executemany(insert, rows)
        total += len(rows)
        target.commit()
        print(f"COPY raw_rows={total}", flush=True)
    return total


def build_bars(source: duckdb.DuckDBPyConnection, target: sqlite3.Connection, cfg: dict[str, Any], label: str, interval: str) -> int:
    src, out = cfg["input"], cfg["output"]
    validate_filter(src["volume_event_filter"])
    query = f"""
        WITH source_rows AS (
            SELECT symbol,
                   {ident(src['event_time_column'])} AS event_time,
                   {ident(src['price_column'])}::DOUBLE AS price,
                   {ident(src['size_column'])}::DOUBLE AS size,
                   trigger_field
            FROM {ident(src['schema'])}.{ident(src['table'])}
            WHERE symbol = ?
              AND _partition_date BETWEEN ?::DATE AND ?::DATE
              AND event = 'l1_trade'
              AND {ident(src['event_time_column'])} IS NOT NULL
              AND {ident(src['price_column'])} > 0
              AND {ident(src['size_column'])} >= 0
        ), bucketed AS (
            SELECT *, time_bucket(INTERVAL '{interval}', event_time) AS bar_start
            FROM source_rows
        )
        SELECT symbol, epoch_ms(bar_start), epoch_ms(bar_start + INTERVAL '{interval}'),
               arg_min(price, event_time), max(price), min(price), arg_max(price, event_time),
               coalesce(sum(size) FILTER (WHERE trigger_field = 'last_size'), 0),
               count(*) FILTER (WHERE trigger_field = 'last_size'), count(*)
        FROM bucketed
        GROUP BY symbol, bar_start
        ORDER BY bar_start
    """
    rows = source.execute(query, [src["symbol"], src["date_start"], src["date_end"]]).fetchall()
    table = ident(out["bars_tables"][label])
    target.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    target.commit()
    print(f"OK interval={label} rows={len(rows)} table={out['bars_tables'][label]}", flush=True)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    cfg = load_config(config_path)
    source_path = resolve(config_path, cfg["input"]["duckdb_path"])
    output_path = resolve(config_path, cfg["output"]["sqlite_path"])
    print({"source": str(source_path), "output": str(output_path), "symbol": cfg["input"]["symbol"], "date_start": cfg["input"]["date_start"], "date_end": cfg["input"]["date_end"], "timezone": cfg["aggregation"]["timezone"]}, flush=True)
    if args.dry_run:
        return
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()
    source = duckdb.connect(str(source_path), read_only=True)
    target = sqlite3.connect(temp_path)
    try:
        target.execute("PRAGMA journal_mode=WAL")
        target.execute("PRAGMA synchronous=NORMAL")
        create_schema(target, cfg)
        target.commit()
        raw_count = copy_raw(source, target, cfg)
        counts = {label: build_bars(source, target, cfg, label, interval) for label, interval in cfg["aggregation"]["intervals"].items()}
        target.execute("PRAGMA optimize")
        target.commit()
    finally:
        target.close()
        source.close()
    os.replace(temp_path, output_path)
    print(f"DONE raw_rows={raw_count} bars={counts} sqlite={output_path}", flush=True)


if __name__ == "__main__":
    main()
