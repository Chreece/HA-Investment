"""Capture the prespecified EUR universe for the November contribution study.

This is an explicit, read-only public market-data download. No user portfolio
or credentials are used. The snapshot is current-vintage history, not a
point-in-time archive. It must not be described as prospective evidence.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from research.fetch_validation_data import LISTINGS, canonical


UNIVERSE = {
    symbol: {**metadata, "category": "etf", "expected_exchanges": ["GER", "XETRA", "XETR"]}
    for symbol, metadata in LISTINGS.items()
}
UNIVERSE.update({
    "BTC-EUR": {"name": "Bitcoin", "sleeve": "crypto", "category": "crypto",
                "inception": None, "expected_exchanges": ["CCC"]},
    "ETH-EUR": {"name": "Ethereum", "sleeve": "crypto", "category": "crypto",
                "inception": None, "expected_exchanges": ["CCC"]},
})


def fetch(symbol: str, *, start: str, completed_through: str):
    metadata = UNIVERSE[symbol]
    start_day = date.fromisoformat(start)
    final_day = date.fromisoformat(completed_through)
    if final_day < start_day:
        raise ValueError("End precedes start")
    first = int(datetime.combine(start_day, datetime.min.time(), timezone.utc).timestamp())
    last = int(datetime.combine(final_day + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol)
    url += "?" + urllib.parse.urlencode({"period1": first, "period2": last, "interval": "1d",
                                       "events": "div,splits", "includeAdjustedClose": "true"})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (HA-Investment research)",
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    captured = datetime.now(timezone.utc).isoformat()
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    if (meta.get("symbol") != symbol or meta.get("currency") != "EUR"
            or meta.get("dataGranularity") != "1d"
            or meta.get("exchangeName") not in metadata["expected_exchanges"]):
        raise ValueError(f"Unverified source identity/currency/exchange/cadence: {symbol}")
    events = result.get("events") or {}
    if events.get("splits") or events.get("dividends"):
        raise ValueError(f"Unimplemented cash distribution or split event: {symbol}")
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    today = datetime.now(zone).date()
    if final_day >= today:
        raise ValueError("Snapshot cutoff must exclude today's possibly incomplete session")
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    if not len(stamps) == len(closes) == len(adjusted):
        raise ValueError(f"Misaligned provider arrays: {symbol}")
    bars, missing = [], []
    for stamp, close, adj in zip(stamps, closes, adjusted):
        session = datetime.fromtimestamp(stamp, zone).date()
        if not start_day <= session <= final_day:
            continue
        if metadata["inception"] is not None and session.isoformat() < metadata["inception"]:
            continue
        if close is None or adj is None:
            missing.append(session.isoformat())
            continue
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v <= 0 for v in (close, adj)):
            raise ValueError(f"Invalid observed price: {symbol} {session}")
        bars.append({"date": session.isoformat(), "ts": stamp,
                     "close": float(close), "adjusted_close": float(adj)})
    if len(bars) < 260 or any(a["date"] >= b["date"] for a, b in zip(bars, bars[1:])):
        raise ValueError(f"Insufficient or unordered observations: {symbol}")
    # For crypto this is observed provider availability, not a claimed protocol
    # creation date. No prices are fabricated before the earliest source bar.
    inception = metadata["inception"] or bars[0]["date"]
    raw = canonical(payload)
    return symbol, {
        "name": metadata["name"], "sleeve": metadata["sleeve"],
        "category": metadata["category"], "inception": inception,
        "inception_basis": "verified_fund_listing" if metadata["inception"] else "first_observed_provider_bar",
        "currency": "EUR", "provider": "yahoo", "exchange_timezone": meta["exchangeTimezoneName"],
        "bars": bars,
        "source": {"url": url, "captured_at_utc": captured,
                   "raw_sha256": hashlib.sha256(raw).hexdigest(),
                   "observed_symbol": meta["symbol"], "observed_exchange": meta["exchangeName"],
                   "missing_sessions": missing, "corporate_actions": events,
                   "completed_through": completed_through, "incomplete_session_excluded": today.isoformat()},
    }, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--completed-through", default="2026-11-05")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite a retained snapshot")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(fetch, symbol, start=args.start, completed_through=args.completed_through)
                   for symbol in sorted(UNIVERSE)]
        for future in futures:
            symbol, row, raw = future.result()
            raw_name = f"{args.output.stem}-{symbol}.json.gz"
            raw_path = args.output.parent / raw_name
            with raw_path.open("xb") as stream:
                stream.write(gzip.compress(raw, mtime=0))
            row["source"]["raw_snapshot"] = raw_name
            rows[symbol] = row
            print(f"{symbol}: {len(row['bars'])} observations {row['bars'][0]['date']}..{row['bars'][-1]['date']}", flush=True)
    document = {
        "schema": "ha-investment-research-source-v1", "kind": "observed_adjusted_history",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(), "currency": "EUR", "assets": rows,
        "preselected_universe": sorted(UNIVERSE), "requested_start": args.start,
        "completed_through": args.completed_through,
        "limitations": [
            "Current-vintage adjusted history, not point-in-time archival data or prospective performance.",
            "Seven surviving listings preselected before strategy results; no delisted-universe reconstruction.",
            "Fund metadata verified for the retained prior study, not reconstructed as historically available metadata.",
            "Crypto observations are provider composite EUR quotes, not a confirmed broker venue or executable bid/ask.",
            "Cash distributions and split events are unsupported and cause capture to fail.",
            "No historical broker fee schedule, bid/ask, taxes, cash interest or inflation series is supplied.",
            "All instruments quote in EUR; no cross-currency execution or historical FX-fee validation is established.",
            "ETF weekday and crypto seven-day calendars require explicit signal and execution scheduling.",
        ],
    }
    with args.output.open("xb") as stream:
        stream.write(canonical(document) + b"\n")
    print(f"snapshot_sha256={hashlib.sha256(args.output.read_bytes()).hexdigest()}", flush=True)


if __name__ == "__main__":
    main()
