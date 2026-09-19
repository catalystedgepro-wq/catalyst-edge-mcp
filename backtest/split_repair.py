#!/usr/bin/env python3
"""split_repair.py — recover the true return across an unadjusted split.

  python3 -m backtest.split_repair --data-repo ../sec-catalyst-data
  python3 -m backtest.split_repair --data-repo ../sec-catalyst-data --write

Excluding these rows (audit_outcomes.py) stops them lying. This repairs them,
which is better: a reverse split is not a missing observation, it is a
known transformation applied to a real return.

THE SIGNATURE
A reverse split is a PERSISTENT level shift. NYMXF sits at $0.0002 on two
separate pick dates, then at $0.02 on the next — exactly 100x, and it stays.
A genuine +9900% move would revert. So the ledger identifies its own splits,
because recurring tickers supply observations on both sides of the shift.

RECOVERING THE RATIO WITHOUT ASSUMING THE ANSWER
The naive route — round the overnight jump to a clean ratio — assumes the
true return is small, which is the thing being measured. Instead the ratio
comes from the ticker's own price series either side of the shift, which is
independent of the jump day:

    ratio = median(closes after) / median(closes before)

snapped to the nearest standard split ratio when it is within tolerance.
Only then is the return recomputed:

    true_return = next_close / (filing_close * ratio) - 1

For NYMXF: 0.02 / (0.0002 * 100) - 1 = 0%. Which is correct — a reverse
split does not make or lose the holder a cent.

Rows with no post-split observation cannot be repaired this way and are
reported, not guessed at.

Stdlib only.
"""

from __future__ import annotations

import argparse
import collections
import csv
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.evaluate import _num, find_ledger, load_ledger  # noqa: E402

# Ratios exchanges actually use for reverse splits.
STANDARD = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200]
SNAP_TOL = 0.25      # accept a ratio within 25% of a standard one
MIN_JUMP = 5.0       # below this, treat a move as a trade, not an action


def log(msg: str = "") -> None:
    print(msg)


def snap(ratio: float) -> int | None:
    best = min(STANDARD, key=lambda s: abs(ratio - s) / s)
    return best if abs(ratio - best) / best <= SNAP_TOL else None


def price_series(rows: list[dict]) -> dict[str, dict[str, float]]:
    ser: dict[str, dict[str, float]] = collections.defaultdict(dict)
    for r in rows:
        c = _num(r.get("filing_day_close"))
        if c and c > 0 and r.get("list_date"):
            ser[r["ticker"]].setdefault(r["list_date"], c)
    return ser


def from_ohlcv(ohlcv: Path, ticker: str, pick_date: str) -> tuple[float | None, str]:
    """True return straight from a split-adjusted price series.

    This is the real repair and needs no ratio at all. Tradier's history
    endpoint (what kronos_pipeline.ohlcv caches) returns split-adjusted
    prices, so both legs are quoted in the same post-split terms and the
    corporate action cancels out of the ratio by construction.

    The ledger's stored pair cannot do this: filing_day_close was captured
    live before the split and next_close after it, so they are in different
    units. That mismatch IS the bug.
    """
    f = ohlcv / f"{ticker}.csv"
    if not f.exists():
        return None, "no OHLCV cache for this ticker"
    try:
        with f.open(newline="", encoding="utf-8") as fh:
            bars = list(csv.DictReader(fh))
    except OSError:
        return None, "OHLCV unreadable"
    i = next((k for k, b in enumerate(bars) if b["date"] >= pick_date), None)
    if i is None or i + 1 >= len(bars):
        return None, "pick date outside the cached window"
    a, b = _num(bars[i].get("close")), _num(bars[i + 1].get("close"))
    if not a or not b or a <= 0:
        return None, "bad cached prices"
    return (b / a - 1) * 100, "from split-adjusted OHLCV"


def infer_ratio(series: dict[str, float], pick_date: str) -> tuple[int | None, str]:
    """Level before vs level after, from the ticker's own observations."""
    before = [v for d, v in series.items() if d <= pick_date]
    after = [v for d, v in series.items() if d > pick_date]
    if not before or not after:
        return None, "no observation after the shift"
    lo, hi = st.median(before), st.median(after)
    if lo <= 0 or hi / lo < MIN_JUMP:
        return None, "no persistent level shift"
    s = snap(hi / lo)
    if s is None:
        return None, f"level shift {hi/lo:.1f}x is not a standard ratio"
    return s, f"{hi/lo:.1f}x -> 1:{s}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--min-move", type=float, default=100.0,
                    help="only examine moves above this %% (default 100)")
    ap.add_argument("--ohlcv-dir", type=Path,
                    help="split-adjusted OHLCV cache (kronos_pipeline.ohlcv). "
                         "When present it is used first and repairs every row "
                         "it covers, with no ratio inference at all.")
    ap.add_argument("--write", action="store_true",
                    help="write outcomes_split_adjusted.csv")
    args = ap.parse_args(argv)

    repo = Path(args.data_repo).expanduser().resolve()
    rows = load_ledger(find_ledger(repo))
    ser = price_series(rows)

    suspect = [r for r in rows
               if (_num(r.get("next_day_close_pct")) or 0) > args.min_move]
    log(f"{len(rows)} picks · {len(suspect)} with a move over +{args.min_move:.0f}%\n")

    repaired = unrepaired = 0
    log(f"  {'ticker':8}{'date':12}{'stored%':>10}{'ratio':>8}{'true%':>9}  evidence")
    seen = set()
    for r in sorted(suspect, key=lambda r: -(_num(r["next_day_close_pct"]) or 0)):
        key = (r["ticker"], r["list_date"])
        fc, nc = _num(r.get("filing_day_close")), _num(r.get("next_close"))
        stored = _num(r.get("next_day_close_pct"))
        # Preferred: read the truth off an adjusted series. Fallback: infer
        # the ratio from the ledger's own persistent level shift.
        direct, dwhy = (from_ohlcv(args.ohlcv_dir, r["ticker"], r["list_date"])
                        if args.ohlcv_dir else (None, ""))
        if direct is not None:
            r["_true_pct"], r["_ratio"] = direct, "ohlcv"
            if key not in seen:
                log(f"  {r['ticker']:8}{r['list_date']:12}{stored:>10.1f}"
                    f"{'ohlcv':>8}{direct:>9.2f}  {dwhy}")
            repaired += 1
            seen.add(key)
            continue
        ratio, why = infer_ratio(ser.get(r["ticker"], {}), r["list_date"])
        if dwhy:
            why = f"{dwhy}; {why}"
        if ratio and fc and nc:
            true = (nc / (fc * ratio) - 1) * 100
            r["_true_pct"], r["_ratio"] = true, ratio
            if key not in seen:
                log(f"  {r['ticker']:8}{r['list_date']:12}{stored:>10.1f}"
                    f"{('1:%d' % ratio):>8}{true:>9.2f}  {why}")
            repaired += 1
        else:
            if key not in seen:
                log(f"  {r['ticker']:8}{r['list_date']:12}{stored:>10.1f}"
                    f"{'-':>8}{'-':>9}  {why}")
            unrepaired += 1
        seen.add(key)

    log(f"\n  repaired {repaired} · could not infer a ratio for {unrepaired}")

    def mean_of(vals):
        v = [x for x in vals if x is not None]
        return st.fmean(v) if v else 0.0

    grp = collections.defaultdict(lambda: ([], []))
    for r in rows:
        a = _num(r.get("alpha_close_pct"))
        if a is None:
            continue
        spy = _num(r.get("spy_close_pct")) or 0.0
        adj = (r["_true_pct"] - spy) if "_true_pct" in r else a
        grp[r["list_name"]][0].append(a)
        grp[r["list_name"]][1].append(adj)

    log(f"\n{'list':24}{'published':>12}{'split-adjusted':>16}{'delta':>10}")
    for k, (raw, adj) in sorted(grp.items(), key=lambda kv: -len(kv[1][0])):
        log(f"{k:24}{mean_of(raw):>12.4f}{mean_of(adj):>16.4f}"
            f"{mean_of(adj)-mean_of(raw):>10.4f}")

    if args.write:
        out = repo / "outcomes_split_adjusted.csv"
        fields = [c for c in rows[0] if not c.startswith("_")] + \
                 ["split_ratio", "alpha_close_pct_adj"]
        tmp = out.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                rec = {c: r.get(c, "") for c in fields if c in r}
                rec["split_ratio"] = r.get("_ratio", "")
                spy = _num(r.get("spy_close_pct")) or 0.0
                rec["alpha_close_pct_adj"] = (
                    f"{r['_true_pct'] - spy:.4f}" if "_true_pct" in r
                    else r.get("alpha_close_pct", ""))
                w.writerow(rec)
        tmp.replace(out)
        log(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
