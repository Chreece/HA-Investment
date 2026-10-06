"""Capture an explicitly dated source snapshot for chronological research.

Network access is opt-in. The replay and tests never download silently. Raw
responses are compressed with a deterministic gzip header and retained beside
their checksums; the provider's current adjusted history is not a vintage,
point-in-time database. All selected share classes accumulate income. Nonzero
dividend or split events fail this bounded experiment instead of simulating
unimplemented cash-flow or nominal-unit adjustments.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
LISTINGS = {
    "XEON.DE": {"name": "Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C", "sleeve": "cash_like", "inception": "2007-05-25"},
    "VAGF.DE": {"name": "Vanguard Global Aggregate Bond UCITS ETF EUR Hedged Accumulating", "sleeve": "aggregate_bond", "inception": "2019-06-20"},
    "SXR8.DE": {"name": "iShares Core S&P 500 UCITS ETF USD Accumulating", "sleeve": "broad_equity", "inception": "2010-05-26"},
    "EUNL.DE": {"name": "iShares Core MSCI World UCITS ETF USD Accumulating", "sleeve": "broad_equity", "inception": "2009-10-20"},
    "VWCE.DE": {"name": "Vanguard FTSE All-World UCITS ETF USD Accumulating", "sleeve": "broad_equity", "inception": "2019-07-23"},
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fetch(symbol):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol)
    url += "?" + urllib.parse.urlencode({"range": "5y", "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (HA-Investment research)", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    captured = datetime.now(timezone.utc).isoformat()
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    if meta.get("symbol") != symbol or meta.get("currency") != "EUR" or meta.get("dataGranularity") != "1d":
        raise ValueError(f"Unexpected source identity/cadence for {symbol}")
    if meta.get("exchangeName") not in {"GER", "XETRA", "XETR"}:
        raise ValueError(f"Unexpected exchange for {symbol}: {meta.get('exchangeName')}")
    events = result.get("events") or {}
    if events.get("splits") or events.get("dividends"):
        raise ValueError(f"This bounded accumulating-fund experiment does not model corporate-action events: {symbol}")
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    if not (len(stamps) == len(closes) == len(adjusted)):
        raise ValueError(f"Misaligned price arrays for {symbol}")
    # The current incomplete trading session is deliberately excluded.
    today = datetime.now(zone).date().isoformat()
    bars, missing = [], []
    for stamp, close, adj in zip(stamps, closes, adjusted):
        session = datetime.fromtimestamp(stamp, zone).date().isoformat()
        if session >= today or session < LISTINGS[symbol]["inception"]:
            continue
        if close is None or adj is None:
            missing.append(session)
            continue
        if isinstance(close, bool) or isinstance(adj, bool) or min(close, adj) <= 0:
            raise ValueError(f"Invalid price for {symbol} on {session}")
        bars.append({"date": session, "ts": stamp, "close": float(close), "adjusted_close": float(adj)})
    if len(bars) < 260 or any(a["date"] >= b["date"] for a, b in zip(bars, bars[1:])):
        raise ValueError(f"Insufficient or nonchronological history for {symbol}")
    raw = canonical(payload)
    return symbol, {
        **LISTINGS[symbol], "category": "etf", "currency": "EUR", "provider": "yahoo",
        "exchange_timezone": meta["exchangeTimezoneName"], "bars": bars,
        "source": {"url": url, "captured_at_utc": captured, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                   "observed_symbol": meta["symbol"], "observed_exchange": meta["exchangeName"],
                   "missing_sessions": missing, "corporate_actions": events,
                   "incomplete_session_excluded": today},
    }, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite a retained source snapshot; choose a new filename.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        for symbol, row, raw in executor.map(fetch, LISTINGS):
            raw_name = f"{args.output.stem}-{symbol}.json.gz"
            raw_path = args.output.parent / raw_name
            if raw_path.exists():
                raise FileExistsError(raw_path)
            raw_path.write_bytes(gzip.compress(raw, mtime=0))
            row["source"]["raw_snapshot"] = raw_name
            rows[symbol] = row
            print(f"{symbol}: {len(row['bars'])} completed daily observations", flush=True)
    document = {
        "schema": "ha-investment-research-source-v1", "kind": "observed_adjusted_history",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(), "currency": "EUR", "assets": rows,
        "limitations": [
            "Current adjusted-history vintage; corrections and retroactive adjustments may differ from what was observable historically.",
            "Universe selected from today's verified surviving listings; no delisted-universe or historical-universe reconstruction.",
            "Listing identities were checked in 2026; this does not establish their point-in-time metadata availability in earlier years.",
            "No dividends or split events were reported for this accumulating-fund snapshot; the fetch fails if such events are present.",
            "Daily close execution is a hypothetical next-session close fill with configured costs, not a promise that a broker would execute at that price.",
            "No taxes, cash interest, historical bid/ask, historical broker schedules or official inflation series are supplied.",
        ],
    }
    args.output.write_bytes(canonical(document) + b"\n")
    print(f"snapshot={args.output} sha256={hashlib.sha256(args.output.read_bytes()).hexdigest()}", flush=True)


if __name__ == "__main__":
    main()
