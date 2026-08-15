from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import urllib.parse
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent

def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    return config

def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill A-share daily bars into SQLite")
    parser.add_argument("--config", type=Path, default=ROOT / "config.ashare_daily.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    source, output = config["source"], config["output"]
    db_path = Path(output["sqlite_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    summaries = {}
    try:
        for interval, dataset in config["datasets"].items():
            date_range = {"start": dataset["start"], "end": dataset["end"]}
            params = {"symbol": source["provider_symbol"], "scale": str(dataset["scale"]), "ma": "no", "datalen": str(dataset["datalen"])}
            url = f"{source['endpoint']}?{urllib.parse.urlencode(params, safe=',')}"
            result = subprocess.run(["curl", "--max-time", "30", "-fsSL", "--retry", "3", url], check=True, capture_output=True, text=True)
            body = result.stdout
            json_start, json_end = body.find("(["), body.rfind("])")
            if json_start < 0 or json_end < json_start:
                raise RuntimeError(f"{interval}: source returned no JSONP rows")
            payload = json.loads(body[json_start + 1:json_end + 1])
            shanghai, rows = ZoneInfo("Asia/Shanghai"), []
            for item in payload:
                item_date = item["day"][:10]
                if not date_range["start"] <= item_date <= date_range["end"]:
                    continue
                if interval == "1d":
                    trading_date = datetime.strptime(item_date, "%Y-%m-%d").date()
                    start = datetime.combine(trading_date, time(9, 30), shanghai)
                    end = datetime.combine(trading_date, time(15, 0), shanghai)
                else:
                    end = datetime.strptime(item["day"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=shanghai)
                    start = end - timedelta(hours=1)
                rows.append((source["symbol"], item["day"], epoch_ms(start), epoch_ms(end),
                             float(item["open"]), float(item["close"]), float(item["high"]), float(item["low"]),
                             int(item["volume"]), float(item["amount"]) if item.get("amount") else None,
                             source["adjustment"], source["volume_unit"], source["name"]))
            if not rows:
                raise RuntimeError(f"{interval}: no rows inside configured date range")
            table = dataset["table"]
            con.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
              symbol TEXT NOT NULL, bar_time TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
              open REAL NOT NULL, close REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
              volume INTEGER NOT NULL, amount REAL, adjustment TEXT NOT NULL, volume_unit TEXT NOT NULL, source TEXT NOT NULL,
              PRIMARY KEY (symbol, bar_time))""")
            con.execute(f"DELETE FROM {table} WHERE symbol = ?", [source["symbol"]])
            con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(13))})", rows)
            summaries[interval] = {"rows": len(rows), "start": rows[0][1], "end": rows[-1][1]}
        con.commit()
    finally:
        con.close()
    print(json.dumps({"db_path": str(db_path), "datasets": summaries}, ensure_ascii=False))

if __name__ == "__main__":
    main()
