#!/usr/bin/env python3
"""ohlcv.py — maintain a daily OHLCV cache from Tradier for Kronos.

Kronos consumes ~512 bars of open/high/low/close/volume per ticker. Catalyst
Edge stores none: convergence_alerts.csv is a one-row-per-ticker daily
snapshot, not a time series. This builds and incrementally refreshes that
history under $CATALYST_DATA_ROOT/ohlcv/<TICKER>.csv.

Stdlib only, matching the rest of the server — this runs in the nightly
pipeline on the droplet, where torch may not be installed at all.

  python3 -m kronos_pipeline.ohlcv --from-convergence   # today's universe
  python3 -m kronos_pipeline.ohlcv AAPL MSFT            # explicit tickers
  python3 -m kronos_pipeline.ohlcv --lookback 800 AAPL  # deeper backfill
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DATA_ROOT = Path(os.environ.get("CATALYST_DATA_ROOT", "/opt/catalyst"))
OHLCV_DIR = DATA_ROOT / "ohlcv"
TRADIER_BASE = "https://api.tradier.com/v1"

# Kronos-small/base cap at max_context=512; keep a margin so a few missing
# sessions still leave a full window.
DEFAULT_LOOKBACK = 600

FIELDS = ["date", "open", "high", "low", "close", "volume"]


def log(msg: str) -> None:
    print(f"[kronos.ohlcv] {msg}", file=sys.stderr, flush=True)


def _load_tradier_token() -> str:
    """Same resolution order as catalyst_mcp.py — never hardcode the token."""
    env = os.environ.get("TRADIER_TOKEN", "").strip()
    if env:
        return env
    for path in (DATA_ROOT / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not path.exists():
            continue
        for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ln.startswith("TRADIER_TOKEN="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class OhlcvError(Exception):
    pass


def _get(path: str, token: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        TRADIER_BASE + path,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json",
                 "User-Agent": "CatalystEdge/1.0 (Python urllib)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_daily(ticker: str, token: str, start: dt.date, end: dt.date) -> list[dict]:
    """Daily bars for [start, end]. Returns [] when Tradier knows no history."""
    q = urllib.parse.urlencode({"symbol": ticker, "interval": "daily",
                                "start": start.isoformat(), "end": end.isoformat()})
    try:
        payload = _get(f"/markets/history?{q}", token)
    except urllib.error.HTTPError as e:
        raise OhlcvError(f"{ticker}: HTTP {e.code} from Tradier") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise OhlcvError(f"{ticker}: {e}") from e

    hist = payload.get("history")
    if not hist:                      # Tradier returns null for unknown symbols
        return []
    days = hist.get("day") or []
    if isinstance(days, dict):        # a single bar comes back unwrapped
        days = [days]

    out = []
    for d in days:
        try:
            row = {"date": str(d["date"]),
                   "open": float(d["open"]), "high": float(d["high"]),
                   "low": float(d["low"]), "close": float(d["close"]),
                   "volume": float(d.get("volume") or 0.0)}
        except (KeyError, TypeError, ValueError):
            continue                  # a malformed bar is skipped, not fatal
        out.append(row)
    return out


def read_cache(ticker: str) -> list[dict]:
    path = OHLCV_DIR / f"{ticker}.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_cache(ticker: str, rows: list[dict]) -> Path:
    """Write atomically — the scorer may be reading this file concurrently."""
    OHLCV_DIR.mkdir(parents=True, exist_ok=True)
    path = OHLCV_DIR / f"{ticker}.csv"
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)
    return path


def merge(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Union by date, newest wins — a late correction to a bar overwrites it."""
    by_date = {r["date"]: r for r in existing}
    by_date.update({r["date"]: r for r in fresh})
    return [by_date[d] for d in sorted(by_date)]


def refresh(ticker: str, token: str, lookback: int = DEFAULT_LOOKBACK) -> tuple[int, int]:
    """Bring one ticker's cache up to date. Returns (total_bars, new_bars)."""
    ticker = ticker.strip().upper()
    cached = read_cache(ticker)
    today = dt.date.today()

    if cached:
        # Re-pull the last few sessions: exchanges restate volume after close.
        last = dt.date.fromisoformat(cached[-1]["date"])
        start = last - dt.timedelta(days=5)
    else:
        # Calendar days, not sessions — ~252 sessions/yr, so pad generously.
        start = today - dt.timedelta(days=int(lookback * 1.5))

    fresh = fetch_daily(ticker, token, start, today)
    if not fresh and not cached:
        raise OhlcvError(f"{ticker}: no history returned (delisted or bad symbol?)")

    merged = merge(cached, fresh)[-lookback:]
    write_cache(ticker, merged)
    return len(merged), max(0, len(merged) - len(cached))


def universe_from_convergence() -> list[str]:
    """Today's scored universe, so the cache tracks what the board holds."""
    path = DATA_ROOT / "convergence_alerts.csv"
    if not path.exists():
        raise OhlcvError(f"{path} not found — pass tickers explicitly")
    with path.open(newline="", encoding="utf-8") as f:
        seen, out = set(), []
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", help="tickers to refresh")
    ap.add_argument("--from-convergence", action="store_true",
                    help="refresh every ticker in convergence_alerts.csv")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                    help=f"bars to retain per ticker (default {DEFAULT_LOOKBACK})")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="seconds between symbols, to stay inside Tradier's rate limit")
    args = ap.parse_args(argv)

    token = _load_tradier_token()
    if not token:
        log("TRADIER_TOKEN not configured (checked $TRADIER_TOKEN and .env)")
        return 2

    tickers = [t.strip().upper() for t in args.tickers if t.strip()]
    if args.from_convergence:
        try:
            tickers = universe_from_convergence() + tickers
        except OhlcvError as e:
            log(str(e))
            return 2
    if not tickers:
        log("no tickers given — pass them, or use --from-convergence")
        return 2

    ok = failed = 0
    for i, t in enumerate(dict.fromkeys(tickers)):
        try:
            total, new = refresh(t, token, args.lookback)
            log(f"{t}: {total} bars (+{new})")
            ok += 1
        except OhlcvError as e:
            log(f"{t}: FAILED — {e}")
            failed += 1
        if i:
            time.sleep(args.sleep)

    log(f"done — {ok} ok, {failed} failed, cache at {OHLCV_DIR}")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
