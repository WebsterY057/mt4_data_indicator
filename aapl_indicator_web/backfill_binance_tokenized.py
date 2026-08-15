#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import yaml


def utc_ms(day: str, add_day: bool = False) -> int:
    value = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if add_day:
        value += timedelta(days=1)
    return int(value.timestamp() * 1000)


def ensure_tables(con: sqlite3.Connection) -> None:
    for table in ("klines_1m", "klines_5m"):
        con.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                inst_id TEXT,
                start_ts INTEGER,
                end_ts INTEGER,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                trade_count INTEGER,
                updated_at_ms INTEGER,
                PRIMARY KEY(inst_id, start_ts)
            )"""
        )
    con.commit()


def get_page(url: str, params: dict) -> list[list]:
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": "data-indicator/1.0"},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise RuntimeError(payload)
    return payload


def rebuild_5m(con: sqlite3.Connection, inst_id: str, start_ms: int, end_ms: int) -> int:
    con.execute(
        "DELETE FROM klines_5m WHERE inst_id=? AND start_ts>=? AND start_ts<?",
        (inst_id, start_ms, end_ms),
    )
    rows = con.execute(
        """SELECT start_ts,open,high,low,close,volume,trade_count
           FROM klines_1m
           WHERE inst_id=? AND start_ts>=? AND start_ts<?
           ORDER BY start_ts""",
        (inst_id, start_ms, end_ms),
    ).fetchall()
    buckets: dict[int, list] = {}
    for ts, op, hi, lo, cl, vol, trades in rows:
        bucket = ts // 300000 * 300000
        if bucket not in buckets:
            buckets[bucket] = [op, hi, lo, cl, vol, trades]
        else:
            value = buckets[bucket]
            value[1] = max(value[1], hi)
            value[2] = min(value[2], lo)
            value[3] = cl
            value[4] += vol
            value[5] += trades
    now = int(time.time() * 1000)
    con.executemany(
        "INSERT INTO klines_5m VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(inst_id, ts, ts + 300000, *value, now) for ts, value in sorted(buckets.items())],
    )
    con.commit()
    return len(buckets)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.binance_tokenized.yaml")
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)

    history = cfg["history"]
    start_ms = utc_ms(history["date_start"])
    end_ms = utc_ms(history["date_end"], add_day=True)
    cursor = start_ms
    db_path = Path(cfg["sqlite_path"]).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    ensure_tables(con)
    con.execute(
        "DELETE FROM klines_1m WHERE inst_id=? AND start_ts>=? AND start_ts<?",
        (cfg["inst_id"], start_ms, end_ms),
    )
    con.commit()

    inserted = 0
    try:
        while cursor < end_ms:
            page = get_page(
                history["rest_url"],
                {
                    "symbol": cfg["inst_id"],
                    "interval": history["interval"],
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": history["page_limit"],
                },
            )
            if not page:
                break
            now = int(time.time() * 1000)
            records = []
            for row in page:
                ts = int(row[0])
                if start_ms <= ts < end_ms:
                    records.append(
                        (
                            cfg["inst_id"], ts, int(row[6]) + 1,
                            float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                            float(row[5]), int(row[8]), now,
                        )
                    )
            con.executemany("INSERT OR REPLACE INTO klines_1m VALUES (?,?,?,?,?,?,?,?,?,?)", records)
            con.commit()
            inserted += len(records)
            next_cursor = int(page[-1][0]) + 60000
            if next_cursor <= cursor:
                raise RuntimeError(f"pagination did not advance: {cursor}")
            cursor = next_cursor
            print(f"BACKFILL rows={inserted} next_ts={cursor}", flush=True)
            time.sleep(float(history["request_interval_seconds"]))

        bars_5m = rebuild_5m(con, cfg["inst_id"], start_ms, end_ms)
        total_1m = con.execute(
            "SELECT count(*) FROM klines_1m WHERE inst_id=? AND start_ts>=? AND start_ts<?",
            (cfg["inst_id"], start_ms, end_ms),
        ).fetchone()[0]
        print(f"DONE inst_id={cfg['inst_id']} rows_1m={total_1m} rows_5m={bars_5m}", flush=True)
    finally:
        con.close()


if __name__ == "__main__":
    main()
