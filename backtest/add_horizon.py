#!/usr/bin/env python3
"""add_horizon.py — add a 20-day, SPY-neutralised return to the ledger.

  python3 -m backtest.add_horizon --data-repo ../sec-catalyst-data --horizon 20

Every outcome column in the ledger is next-day. The Numerai result says the
signal lives at a 20-day, factor-neutralised horizon — so the pipeline is
currently blind to the only place its score has been shown to work.

This computes, per pick, from the OHLCV cache built by kronos_pipeline.ohlcv:

    fwd_<N>d_pct        entry next_open -> close N sessions later
    spy_<N>d_pct        SPY over the same window
    alpha_<N>d_pct      the difference

and writes outcomes_h<N>.csv beside the ledger.

IT INHERITS THE ARTIFACT GUARD, DELIBERATELY. Unadjusted reverse splits put
+9900% rows in the next-day data and carried the entire published mean (see
backtest/audit_outcomes.py). A 20-day window spans MORE corporate actions,
not fewer, so the same rows would poison this column harder. Flagged picks
are marked in an `artifact` column rather than silently dropped, so the
aggregate can be computed either way and the choice is visible.

Requires: an OHLCV cache covering each ticker AND SPY. Run
  python3 -m kronos_pipeline.ohlcv --from-convergence SPY
first, with enough lookback to cover the pick dates.

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.audit_outcomes import classify  # noqa: E402
from backtest.evaluate import _num, find_ledger, load_ledger  # noqa: E402


def log(msg: str = "") -> None:
    print(msg)


def load_bars(ohlcv_dir: Path, ticker: str) -> list[dict]:
    p = ohlcv_dir / f"{ticker}.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def forward(bars: list[dict], pick_date: str, horizon: int):
    """(entry_open, exit_close, exit_date) N sessions after the pick date."""
    idx = next((i for i, b in enumerate(bars) if b["date"] >= pick_date), None)
    if idx is None or idx + horizon >= len(bars):
        return None
    e = _num(bars[idx].get("open"))
    x = _num(bars[idx + horizon].get("close"))
    if not e or not x or e <= 0:
        return None
    return e, x, bars[idx + horizon]["date"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--ohlcv-dir", type=Path,
                    help="default: <data-repo>/ohlcv, else $CATALYST_DATA_ROOT/ohlcv")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--benchmark", default="SPY")
    args = ap.parse_args(argv)

    repo = Path(args.data_repo).expanduser().resolve()
    ohlcv = args.ohlcv_dir or (repo / "ohlcv")
    if not ohlcv.exists():
        import os
        ohlcv = Path(os.environ.get("CATALYST_DATA_ROOT", "/opt/catalyst")) / "ohlcv"
    if not ohlcv.exists():
        log(f"no OHLCV cache at {ohlcv}\n"
            f"run: python3 -m kronos_pipeline.ohlcv --from-convergence {args.benchmark}")
        return 2

    bench = load_bars(ohlcv, args.benchmark)
    if not bench:
        log(f"no bars for {args.benchmark} — the neutralised column needs it.\n"
            f"run: python3 -m kronos_pipeline.ohlcv {args.benchmark}")
        return 2

    rows = load_ledger(find_ledger(repo))
    H = args.horizon
    cache: dict[str, list[dict]] = {}
    out_rows, made, no_bars, short = [], 0, set(), 0

    for r in rows:
        t = (r.get("ticker") or "").upper()
        d0 = r.get("list_date")
        rec = dict(r)
        rec["artifact"] = classify(r) or ""
        rec[f"fwd_{H}d_pct"] = rec[f"spy_{H}d_pct"] = rec[f"alpha_{H}d_pct"] = ""
        rec[f"exit_{H}d_date"] = ""
        if t and d0:
            if t not in cache:
                cache[t] = load_bars(ohlcv, t)
            if not cache[t]:
                no_bars.add(t)
            else:
                f = forward(cache[t], d0, H)
                b = forward(bench, d0, H)
                if f and b:
                    fr = (f[1] - f[0]) / f[0] * 100.0
                    br = (b[1] - b[0]) / b[0] * 100.0
                    rec[f"fwd_{H}d_pct"] = f"{fr:.4f}"
                    rec[f"spy_{H}d_pct"] = f"{br:.4f}"
                    rec[f"alpha_{H}d_pct"] = f"{fr - br:.4f}"
                    rec[f"exit_{H}d_date"] = f[2]
                    made += 1
                else:
                    short += 1
        out_rows.append(rec)

    out = repo / f"outcomes_h{H}.csv"
    tmp = out.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    tmp.replace(out)

    log(f"{made} of {len(rows)} picks got a {H}-day window "
        f"({made/len(rows):.1%})")
    if no_bars:
        log(f"  {len(no_bars)} ticker(s) absent from the OHLCV cache")
    if short:
        log(f"  {short} pick(s) whose {H}-day window has not completed")
    log(f"wrote {out}")

    def agg(sel, label):
        v = [_num(r[f"alpha_{H}d_pct"]) for r in sel if r[f"alpha_{H}d_pct"]]
        v = [x for x in v if x is not None]
        if len(v) < 20:
            return
        log(f"  {label:<34} n={len(v):<6} mean {st.mean(v):+7.3f}%  "
            f"median {st.median(v):+6.2f}%  win {sum(1 for x in v if x>0)/len(v):.0%}")

    log(f"\n{H}-DAY SPY-NEUTRALISED RETURN")
    agg(out_rows, "all picks")
    agg([r for r in out_rows if not r["artifact"]], "artifact rows excluded")
    log("\n  Compare the two lines before quoting either. If they disagree, the")
    log("  headline is being set by a handful of corporate actions again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
