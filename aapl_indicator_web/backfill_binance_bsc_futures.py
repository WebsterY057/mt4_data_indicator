#!/usr/bin/env python3
"""Persist Binance USDT perpetual candles for the supplied BSC contract universe."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import yaml


ROOT = Path(__file__).resolve().parent
FAPI = ""
INTERVAL_MS = {"1m": 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000}


def utc_ms(day: str, next_day: bool = False) -> int:
    value = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if next_day:
        value += timedelta(days=1)
    return int(value.timestamp() * 1000)


def fetch_json(path: str, params: dict[str, object] | None = None) -> object:
    query = urllib.parse.urlencode(params or {})
    url = f"{FAPI}{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers={"User-Agent": "data-indicator/1.0"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.load(response)


def parse_contracts(source: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "`0x" not in line:
            continue
        fields = [field.strip() for field in line.strip().strip("|").split("|")]
        if len(fields) < 4:
            continue
        match = re.fullmatch(r"`(0x[0-9a-fA-F]{40})`", fields[1])
        if not match:
            continue
        rustfs = fields[2]
        alpha_match = re.search(r"(ALPHA_\d+)", rustfs)
        symbol_match = re.search(r"/\s*`?([^`\s]+)`?", rustfs)
        records.append({
            "token": fields[0].upper(),
            "contract_address": match.group(1).lower(),
            "alpha_id": alpha_match.group(1) if alpha_match else "",
            "rustfs_symbol": symbol_match.group(1) if symbol_match else "",
            "dex_pool_address": fields[3],
        })
    if not records:
        raise ValueError(f"no BSC contracts parsed from {source}")
    return records


def binance_contracts() -> dict[str, dict[str, object]]:
    payload = fetch_json("/exchangeInfo")
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected Binance exchangeInfo response")
    matched: dict[str, dict[str, object]] = {}
    for item in payload.get("symbols", []):
        if item.get("contractType") != "PERPETUAL" or item.get("quoteAsset") != "USDT":
            continue
        if item.get("status") != "TRADING":
            continue
        tick_size = next((f.get("tickSize") for f in item.get("filters", []) if f.get("filterType") == "PRICE_FILTER"), None)
        if tick_size is not None:
            matched[str(item["baseAsset"]).upper()] = {"symbol": str(item["symbol"]), "price_tick_size": float(tick_size)}
    return matched


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS contract_universe (
          contract_address TEXT PRIMARY KEY,
          token TEXT NOT NULL,
          alpha_id TEXT NOT NULL,
          rustfs_symbol TEXT NOT NULL,
          dex_pool_address TEXT NOT NULL,
          binance_symbol TEXT NOT NULL,
          price_tick_size REAL NOT NULL,
          source_file TEXT NOT NULL,
          fetched_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS klines_1m (
          contract_address TEXT NOT NULL,
          token TEXT NOT NULL,
          binance_symbol TEXT NOT NULL,
          start_ts INTEGER NOT NULL,
          end_ts INTEGER NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL, quote_volume REAL NOT NULL, trade_count INTEGER NOT NULL,
          PRIMARY KEY (contract_address, start_ts)
        );
        CREATE TABLE IF NOT EXISTS klines_5m (
          contract_address TEXT NOT NULL,
          token TEXT NOT NULL,
          binance_symbol TEXT NOT NULL,
          start_ts INTEGER NOT NULL,
          end_ts INTEGER NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL, quote_volume REAL NOT NULL, trade_count INTEGER NOT NULL,
          PRIMARY KEY (contract_address, start_ts)
        );
        CREATE TABLE IF NOT EXISTS klines_15m (
          contract_address TEXT NOT NULL,
          token TEXT NOT NULL,
          binance_symbol TEXT NOT NULL,
          start_ts INTEGER NOT NULL,
          end_ts INTEGER NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL, quote_volume REAL NOT NULL, trade_count INTEGER NOT NULL,
          PRIMARY KEY (contract_address, start_ts)
        );
        CREATE INDEX IF NOT EXISTS idx_klines_1m_contract_ts ON klines_1m(contract_address, start_ts);
        CREATE INDEX IF NOT EXISTS idx_klines_5m_contract_ts ON klines_5m(contract_address, start_ts);
        CREATE INDEX IF NOT EXISTS idx_klines_15m_contract_ts ON klines_15m(contract_address, start_ts);
        CREATE TABLE IF NOT EXISTS sync_audit (
          run_at_utc TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT NOT NULL,
          requested_contracts INTEGER NOT NULL, mapped_contracts INTEGER NOT NULL,
          loaded_contracts INTEGER NOT NULL, failed_contracts INTEGER NOT NULL,
          details_json TEXT NOT NULL
        );
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(contract_universe)")}
    if "price_tick_size" not in columns:
        con.execute("ALTER TABLE contract_universe ADD COLUMN price_tick_size REAL NOT NULL DEFAULT 0.0")
    con.commit()


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list[object]]:
    cursor = start_ms
    rows: list[list[object]] = []
    while cursor < end_ms:
        page = fetch_json("/klines", {
            "symbol": symbol, "interval": "1m", "startTime": cursor,
            "endTime": end_ms - 1, "limit": 1500,
        })
        if not isinstance(page, list):
            raise RuntimeError(f"{symbol}: unexpected klines response: {page}")
        if not page:
            break
        rows.extend(row for row in page if start_ms <= int(row[0]) < end_ms)
        next_cursor = int(page[-1][0]) + INTERVAL_MS["1m"]
        if next_cursor <= cursor:
            raise RuntimeError(f"{symbol}: pagination did not advance")
        cursor = next_cursor
        time.sleep(0.03)
    return rows


def write_1m(con: sqlite3.Connection, record: dict[str, str], rows: list[list[object]], start_ms: int, end_ms: int) -> int:
    con.execute("DELETE FROM klines_1m WHERE contract_address=? AND start_ts>=? AND start_ts<?", (record["contract_address"], start_ms, end_ms))
    values = [(
        record["contract_address"], record["token"], record["binance_symbol"], int(row[0]), int(row[6]) + 1,
        float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7]), int(row[8]),
    ) for row in rows]
    con.executemany("INSERT INTO klines_1m VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values)
    return len(values)


def rebuild_interval(con: sqlite3.Connection, record: dict[str, str], start_ms: int, end_ms: int, interval: str) -> int:
    address = record["contract_address"]
    table = f"klines_{interval}"
    con.execute(f"DELETE FROM {table} WHERE contract_address=? AND start_ts>=? AND start_ts<?", (address, start_ms, end_ms))
    rows = con.execute("""SELECT start_ts, open, high, low, close, volume, quote_volume, trade_count
                          FROM klines_1m WHERE contract_address=? AND start_ts>=? AND start_ts<? ORDER BY start_ts""",
                       (address, start_ms, end_ms)).fetchall()
    buckets: dict[int, list[float | int]] = {}
    for ts, op, hi, lo, cl, vol, quote, trades in rows:
        bucket = int(ts) // INTERVAL_MS[interval] * INTERVAL_MS[interval]
        if bucket not in buckets:
            buckets[bucket] = [op, hi, lo, cl, vol, quote, trades]
        else:
            item = buckets[bucket]
            item[1] = max(float(item[1]), hi); item[2] = min(float(item[2]), lo); item[3] = cl
            item[4] = float(item[4]) + vol; item[5] = float(item[5]) + quote; item[6] = int(item[6]) + trades
    con.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (address, record["token"], record["binance_symbol"], ts, ts + INTERVAL_MS[interval], *values)
        for ts, values in sorted(buckets.items())
    ])
    return len(buckets)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    source = config.get("source", {})
    window = config.get("window", {})
    output = config.get("output", {})
    assets = config.get("assets", [])
    if not isinstance(source, dict) or not isinstance(window, dict) or not isinstance(output, dict) or not isinstance(assets, list):
        raise ValueError("source, window, output, and assets must be configured")
    global FAPI
    FAPI = str(source["fapi_base_url"]).rstrip("/")
    database = Path(str(output["database"])).expanduser()
    date_start, date_end = str(window["date_start"]), str(window["date_end"])
    start_ms, end_ms = utc_ms(date_start), utc_ms(date_end, next_day=True)
    records = []
    for asset in assets:
        if not isinstance(asset, dict) or not asset.get("token"):
            raise ValueError("each asset needs a token")
        token = str(asset["token"]).upper()
        records.append({
            "token": token,
            "contract_address": str(asset.get("instrument_id", f"binance:{token.lower()}usdt")),
            "alpha_id": str(asset.get("alpha_id", "")),
            "rustfs_symbol": str(asset.get("rustfs_symbol", "")),
            "dex_pool_address": str(asset.get("dex_pool_address", "")),
        })
    if not records:
        raise ValueError("assets must not be empty")
    exchange = binance_contracts()
    mapped, unresolved = [], []
    for record in records:
        market = exchange.get(record["token"])
        if market:
            mapped.append({**record, "binance_symbol": str(market["symbol"]), "price_tick_size": float(market["price_tick_size"])})
        else:
            unresolved.append(record)
    if unresolved:
        raise RuntimeError(f"unmapped BSC token(s): {[item['token'] for item in unresolved]}")

    database.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(database)
    ensure_tables(con)
    now = datetime.now(timezone.utc).isoformat()
    addresses = [record["contract_address"] for record in mapped]
    placeholders = ",".join("?" for _ in addresses)
    for table in ("klines_1m", "klines_5m", "klines_15m", "contract_universe"):
        con.execute(f"DELETE FROM {table} WHERE contract_address NOT IN ({placeholders})", addresses)
    con.executemany("""INSERT INTO contract_universe (contract_address,token,alpha_id,rustfs_symbol,dex_pool_address,binance_symbol,price_tick_size,source_file,fetched_at_utc) VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(contract_address) DO UPDATE SET token=excluded.token, alpha_id=excluded.alpha_id,
                       rustfs_symbol=excluded.rustfs_symbol, dex_pool_address=excluded.dex_pool_address,
                       binance_symbol=excluded.binance_symbol, price_tick_size=excluded.price_tick_size, source_file=excluded.source_file, fetched_at_utc=excluded.fetched_at_utc""",
                    [(r["contract_address"], r["token"], r["alpha_id"], r["rustfs_symbol"], r["dex_pool_address"], r["binance_symbol"], r["price_tick_size"], str(args.config), now) for r in mapped])
    con.commit()
    completed, failures, total_1m, total_5m, total_15m = 0, [], 0, 0, 0
    try:
        for index, record in enumerate(mapped, start=1):
            try:
                rows = fetch_klines(record["binance_symbol"], start_ms, end_ms)
                total_1m += write_1m(con, record, rows, start_ms, end_ms)
                total_5m += rebuild_interval(con, record, start_ms, end_ms, "5m")
                total_15m += rebuild_interval(con, record, start_ms, end_ms, "15m")
                con.commit(); completed += 1
                print(f"{index}/{len(mapped)} {record['token']} {record['contract_address']} rows_1m={len(rows)}", flush=True)
            except Exception as exc:
                con.rollback(); failures.append({"contract_address": record["contract_address"], "token": record["token"], "error": str(exc)})
                print(f"ERROR {record['token']} {exc}", flush=True)
        audit = {"database": str(database), "date_start": date_start, "date_end": date_end,
                 "requested_contracts": len(records), "mapped_contracts": len(mapped), "loaded_contracts": completed,
                 "failed_contracts": len(failures), "rows_1m": total_1m, "rows_5m": total_5m, "rows_15m": total_15m, "failures": failures}
        con.execute("INSERT INTO sync_audit VALUES (?,?,?,?,?,?,?,?)", (now, date_start, date_end, len(records), len(mapped), completed, len(failures), json.dumps(audit, ensure_ascii=False)))
        con.commit()
        print(json.dumps(audit, ensure_ascii=False), flush=True)
        if failures:
            raise RuntimeError(f"{len(failures)} contract(s) failed")
    finally:
        con.close()


if __name__ == "__main__":
    main()
