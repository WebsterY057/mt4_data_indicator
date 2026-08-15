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

import yaml
import certifi


def utc_ms(day: str, add_day: bool = False) -> int:
    value = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if add_day:
        value += timedelta(days=1)
    return int(value.timestamp() * 1000)


def ensure_tables(con: sqlite3.Connection) -> None:
    for table in ("klines_1m", "klines_5m"):
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} (inst_id TEXT, start_ts INTEGER, end_ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL, trade_count INTEGER, updated_at_ms INTEGER, PRIMARY KEY(inst_id, start_ts))")
    con.commit()


def get_page(url: str, params: dict) -> list[list[str]]:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "data-indicator/1.0"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        payload = json.load(response)
    if payload.get("code") != "0":
        raise RuntimeError(payload)
    return payload.get("data", [])


def rebuild_5m(con: sqlite3.Connection, inst_id: str, start_ms: int, end_ms: int) -> int:
    con.execute("DELETE FROM klines_5m WHERE inst_id=? AND start_ts>=? AND start_ts<?", (inst_id, start_ms, end_ms))
    rows = con.execute("SELECT start_ts,open,high,low,close,volume FROM klines_1m WHERE inst_id=? AND start_ts>=? AND start_ts<? ORDER BY start_ts", (inst_id, start_ms, end_ms)).fetchall()
    buckets: dict[int, list] = {}
    for ts, op, hi, lo, cl, vol in rows:
        bucket = ts // 300000 * 300000
        if bucket not in buckets:
            buckets[bucket] = [op, hi, lo, cl, vol]
        else:
            b = buckets[bucket]
            b[1], b[2], b[3], b[4] = max(b[1], hi), min(b[2], lo), cl, b[4] + vol
    now = int(time.time() * 1000)
    con.executemany("INSERT INTO klines_5m VALUES (?,?,?,?,?,?,?,?,?,?)", [(inst_id, ts, ts+300000, *v, 0, now) for ts, v in sorted(buckets.items())])
    con.commit()
    return len(buckets)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.okx_tokenized.yaml")
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)
    history = cfg["history"]
    start_ms = utc_ms(history["date_start"])
    end_ms = utc_ms(history["date_end"], add_day=True)
    cursor = end_ms
    con = sqlite3.connect(Path(cfg["sqlite_path"]).expanduser(), timeout=30)
    ensure_tables(con)
    inserted = 0
    try:
        while cursor > start_ms:
            page = get_page(history["rest_url"], {"instId": cfg["inst_id"], "bar": history["bar"], "after": cursor, "limit": history["page_limit"]})
            if not page:
                break
            now = int(time.time() * 1000)
            records = []
            oldest = cursor
            for row in page:
                ts = int(row[0]); oldest = min(oldest, ts)
                if start_ms <= ts < end_ms:
                    records.append((cfg["inst_id"], ts, ts+60000, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), 0, now))
            con.executemany("INSERT OR REPLACE INTO klines_1m VALUES (?,?,?,?,?,?,?,?,?,?)", records)
            con.commit(); inserted += len(records)
            if inserted % 5000 < len(records):
                print(f"BACKFILL rows={inserted} oldest_ts={oldest}", flush=True)
            if oldest >= cursor:
                break
            cursor = oldest
            if oldest < start_ms:
                break
            time.sleep(float(history["request_interval_seconds"]))
        bars5 = rebuild_5m(con, cfg["inst_id"], start_ms, end_ms)
        total1 = con.execute("SELECT count(*) FROM klines_1m WHERE inst_id=? AND start_ts>=? AND start_ts<?", (cfg["inst_id"], start_ms, end_ms)).fetchone()[0]
        print(f"DONE inst_id={cfg['inst_id']} rows_1m={total1} rows_5m={bars5}", flush=True)
    finally:
        con.close()


if __name__ == "__main__":
    main()
