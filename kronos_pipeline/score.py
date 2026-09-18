#!/usr/bin/env python3
"""score.py — run Kronos over the OHLCV cache and emit kronos_forecasts.csv.

Offline only. Needs torch and a checkout of https://github.com/shiyu-coder/Kronos;
the MCP server never imports this and stays stdlib-only.

  python3 -m kronos_pipeline.score --kronos-src /opt/kronos --from-convergence

WHY SAMPLES, NOT A POINT FORECAST
Kronos is generative. `predict(sample_count=N)` averages the N paths internally
(model/kronos.py: `preds = np.mean(preds, axis=1)`), which throws away exactly
the part worth having. A single averaged path tells you a number; a spread of
paths tells you a probability and a confidence. So this passes the same window
as N separate series to `predict_batch` with sample_count=1 — N independent
paths in one batched pass — and reports the distribution.

The headline output is `p_up`: the fraction of sampled paths closing above the
last actual close. That is a probability in [0,1], which composes with the
existing convergence score far more naturally than a predicted price does.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

DATA_ROOT = Path(os.environ.get("CATALYST_DATA_ROOT", "/opt/catalyst"))
OHLCV_DIR = DATA_ROOT / "ohlcv"
OUT_FILE = DATA_ROOT / "kronos_forecasts.csv"

# Kronos-small / Kronos-base are trained at max_context=512.
MAX_CONTEXT = 512
FIELDS = ["ticker", "as_of_bar", "horizon_days", "last_close", "pred_close",
          "pred_return_pct", "p_up", "ret_p10_pct", "ret_p90_pct", "samples",
          "invariant_repairs", "model", "generated_at"]


def log(msg: str) -> None:
    print(f"[kronos.score] {msg}", file=sys.stderr, flush=True)


def load_window(ticker: str, lookback: int):
    """Last `lookback` bars for a ticker as a Kronos-shaped frame."""
    import pandas as pd

    path = OHLCV_DIR / f"{ticker}.csv"
    if not path.exists():
        raise FileNotFoundError(f"no OHLCV cache for {ticker} — run kronos_pipeline.ohlcv")
    df = pd.read_csv(path)
    if len(df) < lookback:
        raise ValueError(f"{ticker}: {len(df)} bars cached, need {lookback}")
    df = df.tail(lookback).reset_index(drop=True)
    stamps = pd.to_datetime(df["date"])
    # Kronos wants price/volume columns only; it derives `amount` itself.
    return df[["open", "high", "low", "close", "volume"]].astype(float), stamps


def repair_ohlc(frame):
    """Clamp sampled bars back inside high >= max(o,c) >= min(o,c) >= low.

    Kronos samples OHLC from a discrete codebook with nothing enforcing the
    ordering, so a path can contain a bar whose low exceeds its high. Rare with
    trained weights, not impossible, and one nonsense bar silently poisons a
    signal layer. Returns (repaired_frame, n_repaired).
    """
    body_hi = frame[["open", "close"]].max(axis=1)
    body_lo = frame[["open", "close"]].min(axis=1)
    bad = (frame["high"] < body_hi) | (frame["low"] > body_lo)
    if bad.any():
        frame = frame.copy()
        frame["high"] = frame["high"].clip(lower=body_hi)   # high >= max(o,c)
        frame["low"] = frame["low"].clip(upper=body_lo)     # low  <= min(o,c)
    return frame, int(bad.sum())


def forecast(predictor, df, stamps, horizon: int, samples: int, temperature: float):
    """N independent sampled paths for one ticker. Returns a result dict."""
    import numpy as np
    import pandas as pd

    last_close = float(df["close"].iloc[-1])
    last_date = stamps.iloc[-1]
    # Business days ignore exchange holidays; Kronos only reads weekday/day/month
    # time features from these, so a holiday shifts a feature, not the series.
    future = pd.Series(pd.bdate_range(last_date + pd.Timedelta(days=1), periods=horizon))

    out = predictor.predict_batch(
        df_list=[df] * samples,
        x_timestamp_list=[stamps] * samples,
        y_timestamp_list=[future] * samples,
        pred_len=horizon, T=temperature, top_p=0.9, sample_count=1, verbose=False)

    closes, repairs = [], 0
    for path in out:
        path, n = repair_ohlc(path)
        repairs += n
        closes.append(float(path["close"].iloc[-1]))

    closes = np.asarray(closes, dtype=float)
    rets = (closes - last_close) / last_close * 100.0
    return {
        "as_of_bar": str(last_date.date()),
        "horizon_days": horizon,
        "last_close": round(last_close, 4),
        "pred_close": round(float(np.median(closes)), 4),
        "pred_return_pct": round(float(np.median(rets)), 4),
        "p_up": round(float((rets > 0).mean()), 4),
        "ret_p10_pct": round(float(np.percentile(rets, 10)), 4),
        "ret_p90_pct": round(float(np.percentile(rets, 90)), 4),
        "samples": samples,
        "invariant_repairs": repairs,
    }


def universe_from_convergence() -> list[str]:
    path = DATA_ROOT / "convergence_alerts.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — pass tickers explicitly")
    with path.open(newline="", encoding="utf-8") as f:
        seen, out = set(), []
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def write_forecasts(rows: list[dict]) -> Path:
    """Atomic write — the MCP server reads this file on every request."""
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(OUT_FILE)
    return OUT_FILE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--from-convergence", action="store_true")
    ap.add_argument("--kronos-src", default=os.environ.get("KRONOS_SRC", "/opt/kronos"),
                    help="checkout of shiyu-coder/Kronos (for `import model`)")
    ap.add_argument("--model", default="NeoQuasar/Kronos-small",
                    help="HF repo id, or a local directory")
    ap.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    ap.add_argument("--lookback", type=int, default=400)
    ap.add_argument("--horizon", type=int, default=5, help="trading days ahead")
    ap.add_argument("--samples", type=int, default=20,
                    help="sampled paths per ticker; more = tighter p_up, linearly slower")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--device", default=None, help="cpu / cuda:0 (default: auto)")
    args = ap.parse_args(argv)

    if args.lookback > MAX_CONTEXT:
        log(f"lookback {args.lookback} exceeds max_context {MAX_CONTEXT} — Kronos will truncate")

    sys.path.insert(0, args.kronos_src)
    try:
        from model import Kronos, KronosPredictor, KronosTokenizer
    except ImportError as e:
        log(f"cannot import Kronos from {args.kronos_src}: {e}")
        log("clone https://github.com/shiyu-coder/Kronos and pass --kronos-src")
        return 2

    tickers = [t.strip().upper() for t in args.tickers if t.strip()]
    if args.from_convergence:
        try:
            tickers = universe_from_convergence() + tickers
        except FileNotFoundError as e:
            log(str(e))
            return 2
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        log("no tickers given — pass them, or use --from-convergence")
        return 2

    log(f"loading {args.model} + {args.tokenizer}")
    try:
        tokenizer = KronosTokenizer.from_pretrained(args.tokenizer)
        model = Kronos.from_pretrained(args.model)
    except Exception as e:  # noqa: BLE001 — network, auth and format errors all land here
        log(f"could not load weights: {type(e).__name__}: {e}")
        log("if huggingface.co is unreachable, download once and pass a local path")
        return 2

    predictor = KronosPredictor(model, tokenizer, device=args.device,
                                max_context=MAX_CONTEXT)
    log(f"device={predictor.device} tickers={len(tickers)} "
        f"samples={args.samples} horizon={args.horizon}d")

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows, failed = [], 0
    for t in tickers:
        try:
            df, stamps = load_window(t, args.lookback)
            res = forecast(predictor, df, stamps, args.horizon, args.samples,
                           args.temperature)
        except (FileNotFoundError, ValueError) as e:
            log(f"{t}: skipped — {e}")
            failed += 1
            continue
        except Exception as e:  # noqa: BLE001 — one bad ticker must not kill the run
            log(f"{t}: FAILED — {type(e).__name__}: {e}")
            failed += 1
            continue
        rows.append({"ticker": t, "model": args.model, "generated_at": now, **res})
        log(f"{t}: p_up={res['p_up']:.2f} ret={res['pred_return_pct']:+.2f}% "
            f"[{res['ret_p10_pct']:+.1f}, {res['ret_p90_pct']:+.1f}] "
            f"repairs={res['invariant_repairs']}")

    if not rows:
        log("no forecasts produced — leaving the previous kronos_forecasts.csv in place")
        return 1

    write_forecasts(rows)
    log(f"wrote {len(rows)} forecasts to {OUT_FILE} ({failed} skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
