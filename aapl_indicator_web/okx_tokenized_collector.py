#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import ssl
import time
from pathlib import Path

import certifi
import websockets
import yaml


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS raw_trades (inst_id TEXT, trade_id TEXT, px REAL, sz REAL, side TEXT, ts INTEGER, recv_time_ms INTEGER, PRIMARY KEY(inst_id, trade_id))")
    for table in ("klines_1m", "klines_5m"):
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} (inst_id TEXT, start_ts INTEGER, end_ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL, trade_count INTEGER, updated_at_ms INTEGER, PRIMARY KEY(inst_id, start_ts))")
    con.commit()
    return con


def save_trade(con: sqlite3.Connection, inst: str, item: dict, intervals: dict[str, int]) -> None:
    ts, px, size = int(item["ts"]), float(item["px"]), float(item["sz"])
    cur = con.execute("INSERT OR IGNORE INTO raw_trades VALUES (?,?,?,?,?,?,?)", (inst, str(item["tradeId"]), px, size, str(item["side"]), ts, int(time.time()*1000)))
    if cur.rowcount == 0:
        return
    now = int(time.time()*1000)
    for interval, width in intervals.items():
        start = ts // width * width
        table = f"klines_{interval}"
        con.execute(f"""INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(inst_id,start_ts) DO UPDATE SET
          high=max(high,excluded.high), low=min(low,excluded.low), close=excluded.close,
          volume=volume+excluded.volume, trade_count=trade_count+1, updated_at_ms=excluded.updated_at_ms""",
          (inst, start, start+width, px, px, px, px, size, 1, now))
    con.commit()


async def collect(cfg: dict) -> None:
    con = open_db(Path(cfg["sqlite_path"]).expanduser())
    delay = float(cfg["reconnect_initial_seconds"])
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        while True:
            try:
                async with websockets.connect(cfg["okx_ws_url"], ssl=context, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"op":"subscribe","args":[{"channel":"trades","instId":cfg["inst_id"]}]}))
                    print(f"CONNECTED inst_id={cfg['inst_id']}", flush=True)
                    delay = float(cfg["reconnect_initial_seconds"])
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("event") == "error":
                            raise RuntimeError(msg)
                        for item in msg.get("data", []):
                            if item.get("instId") == cfg["inst_id"]:
                                save_trade(con, cfg["inst_id"], item, cfg["intervals"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"RECONNECT error={exc!r} delay={delay}", flush=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, float(cfg["reconnect_max_seconds"]))
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.okx_tokenized.yaml")
    args = parser.parse_args()
    asyncio.run(collect(load_config(Path(args.config))))


if __name__ == "__main__":
    main()
