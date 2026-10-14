#!/usr/bin/env python3
"""Build local 15-minute SQLite bars from existing 1-minute tables."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

import yaml


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"config is not a mapping: {path}")
    return config


def resolve(config_path: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def aggregate_generic(con: sqlite3.Connection, source: str, target: str, interval_ms: int) -> int:
    src, dst = quote_identifier(source), quote_identifier(target)
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({src})")}
    extra_name = "source_event_count" if "source_event_count" in columns else "updated_at_ms"
    extra_expr = "sum(source_event_count)" if extra_name == "source_event_count" else "max(updated_at_ms)"
    con.execute(f"DROP TABLE IF EXISTS {dst}")
    con.execute(f"""
        CREATE TABLE {dst} (
            inst_id TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume REAL NOT NULL, trade_count INTEGER NOT NULL, {quote_identifier(extra_name)} INTEGER NOT NULL,
            PRIMARY KEY(inst_id, start_ts)
        )
    """)
    con.execute(f"""
        INSERT INTO {dst}
        WITH ranked AS (
            SELECT *, (start_ts / ?) * ? AS bucket,
                   row_number() OVER (PARTITION BY inst_id, (start_ts / ?) ORDER BY start_ts) AS first_rn,
                   row_number() OVER (PARTITION BY inst_id, (start_ts / ?) ORDER BY start_ts DESC) AS last_rn
            FROM {src}
        )
        SELECT inst_id, bucket, bucket + ?,
               max(CASE WHEN first_rn=1 THEN open END), max(high), min(low),
               max(CASE WHEN last_rn=1 THEN close END), sum(volume), sum(trade_count), {extra_expr}
        FROM ranked GROUP BY inst_id, bucket ORDER BY inst_id, bucket
    """, (interval_ms, interval_ms, interval_ms, interval_ms, interval_ms))
    return int(con.execute(f"SELECT count(*) FROM {dst}").fetchone()[0])


def aggregate_dukascopy(con: sqlite3.Connection, source: str, target: str, interval_ms: int) -> int:
    src, dst = quote_identifier(source), quote_identifier(target)
    con.execute(f"DROP TABLE IF EXISTS {dst}")
    con.execute(f"CREATE TABLE {dst} AS SELECT * FROM {src} WHERE 0")
    con.execute(f"CREATE UNIQUE INDEX {quote_identifier('idx_' + target + '_symbol_time')} ON {dst}(symbol, window_start_ms_utc)")
    con.execute(f"""
        INSERT INTO {dst}
        WITH ranked AS (
            SELECT *, (window_start_ms_utc / ?) * ? AS bucket,
                   row_number() OVER (PARTITION BY symbol, (window_start_ms_utc / ?) ORDER BY window_start_ms_utc) AS first_rn,
                   row_number() OVER (PARTITION BY symbol, (window_start_ms_utc / ?) ORDER BY window_start_ms_utc DESC) AS last_rn
            FROM {src}
        )
        SELECT symbol, '15m', bucket, datetime(bucket/1000, 'unixepoch'),
               bucket + ?, datetime((bucket+?)/1000, 'unixepoch'),
               max(CASE WHEN first_rn=1 THEN bid_open END), max(bid_high), min(bid_low), max(CASE WHEN last_rn=1 THEN bid_close END),
               max(CASE WHEN first_rn=1 THEN ask_open END), max(ask_high), min(ask_low), max(CASE WHEN last_rn=1 THEN ask_close END),
               max(CASE WHEN first_rn=1 THEN mid_open END), max(mid_high), min(mid_low), max(CASE WHEN last_rn=1 THEN mid_close END),
               max(CASE WHEN first_rn=1 THEN spread_open END), max(spread_high), min(spread_low), max(CASE WHEN last_rn=1 THEN spread_close END),
               sum(spread_avg*tick_count)/nullif(sum(tick_count),0), sum(bid_volume_sum), sum(ask_volume_sum), sum(tick_count),
               min(first_tick_ms_utc), max(last_tick_ms_utc), datetime('now')
        FROM ranked GROUP BY symbol, bucket ORDER BY symbol, bucket
    """, (interval_ms, interval_ms, interval_ms, interval_ms, interval_ms, interval_ms))
    return int(con.execute(f"SELECT count(*) FROM {dst}").fetchone()[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    interval_ms = int(config["interval"]["milliseconds"])
    for item in config["databases"]:
        path = resolve(config_path, item["path"])
        print(f"PLAN name={item['name']} database={path} source={item['source_table']} target={item['target_table']}", flush=True)
        if args.dry_run:
            continue
        if not path.is_file():
            raise FileNotFoundError(path)
        con = sqlite3.connect(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            if item["profile"] == "generic_klines":
                rows = aggregate_generic(con, item["source_table"], item["target_table"], interval_ms)
            elif item["profile"] == "dukascopy_bars":
                rows = aggregate_dukascopy(con, item["source_table"], item["target_table"], interval_ms)
            else:
                raise ValueError(f"unsupported profile: {item['profile']}")
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        print(f"OK name={item['name']} interval=15m rows={rows} table={item['target_table']}", flush=True)


if __name__ == "__main__":
    main()
