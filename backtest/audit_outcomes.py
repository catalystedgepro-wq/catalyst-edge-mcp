#!/usr/bin/env python3
"""audit_outcomes.py — find unadjusted corporate actions in the outcome ledger.

  python3 -m backtest.audit_outcomes --data-repo ../sec-catalyst-data
  python3 -m backtest.audit_outcomes --data-repo ../sec-catalyst-data --write-clean

WHAT THIS EXISTS FOR
The published track record reports avg_alpha_close_pct of +1.60% on
sec_top_gappers. That figure is arithmetically correct — it matches the raw
rows exactly — but it is not a performance claim, because the rows include
unadjusted reverse splits:

    NYMXF  $0.0002 -> $0.0200   +9900%
    NFE    $0.3300 -> $12.7700  +3770%
    ALP    $0.0990 -> $3.7100   +3647%

A reverse split multiplies the price and divides the share count; the holder's
value does not change. Booked as a return it is a fabricated gain, and 19 such
rows out of 13,760 carry the entire positive mean. Drop them and the mean is
-0.288%. Apply a $1 price floor and it is -0.149%.

Two lessons worth keeping:

  A mean over fat-tailed, unadjusted price data is not a measurement. Six rows
  in ten thousand decided the headline number.

  This is precisely why the Numerai result holds while the internal mean does
  not. Numerai RANKS predictions, so a +9900% artifact is just "rank 1 of
  1900" and cannot move the score. Rank-based evaluation is immune to exactly
  the bug that broke the internal average.

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.evaluate import _num, find_ledger, load_ledger  # noqa: E402

# A one-day move this large is a corporate action or a bad print, not a trade.
MOVE_FLAG_PCT = 100.0
# Below this the quoted price is not investable and rounding dominates.
PENNY = 1.00
SUBPENNY = 0.01


def log(msg: str = "") -> None:
    print(msg)


def classify(r: dict) -> str | None:
    """Why this row should not be counted as a realised return."""
    px = _num(r.get("filing_day_close"))
    mv = _num(r.get("next_day_close_pct"))
    if px is None or mv is None:
        return None
    if px < SUBPENNY:
        return "sub-penny price"
    if mv > MOVE_FLAG_PCT and px < PENNY:
        return "reverse-split signature (sub-$1, >+100%)"
    if mv > MOVE_FLAG_PCT:
        return "implausible one-day move"
    if mv < -90:
        return "implausible one-day collapse"
    return None


def summarise(rows: list[dict], target: str) -> dict | None:
    v = [_num(r.get(target)) for r in rows]
    v = [x for x in v if x is not None]
    if not v:
        return None
    return {"n": len(v), "mean": st.mean(v), "median": st.median(v),
            "win": sum(1 for x in v if x > 0) / len(v)}


def line(label: str, s: dict | None) -> None:
    if s:
        log(f"  {label:<40} n={s['n']:<6} mean {s['mean']:+7.3f}%  "
            f"median {s['median']:+6.2f}%  win {s['win']:.0%}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--list-name", default="sec_top_gappers")
    ap.add_argument("--target", default="alpha_close_pct")
    ap.add_argument("--price-floor", type=float, default=PENNY)
    ap.add_argument("--write-clean", action="store_true",
                    help="write outcomes_clean.csv with flagged rows removed")
    args = ap.parse_args(argv)

    repo = Path(args.data_repo).expanduser().resolve()
    try:
        path = find_ledger(repo)
    except FileNotFoundError as e:
        log(str(e))
        return 2
    allrows = load_ledger(path)
    rows = [r for r in allrows if r.get("list_name") == args.list_name]
    if not rows:
        log(f"no rows for list {args.list_name}")
        return 2

    flagged = [(r, why) for r in rows if (why := classify(r))]
    log(f"ledger: {path.relative_to(repo)} · list {args.list_name} · {len(rows)} picks")
    log(f"\n{len(flagged)} row(s) flagged as not-a-return ({len(flagged)/len(rows):.2%}):\n")
    log(f"  {'ticker':10}{'date':12}{'close':>11}{'next':>11}{'move%':>11}  reason")
    for r, why in sorted(flagged, key=lambda t: -(_num(t[0]['next_day_close_pct']) or 0))[:20]:
        log(f"  {r['ticker']:10}{r['list_date']:12}"
            f"{_num(r['filing_day_close']):>11.4f}{_num(r['next_close']):>11.4f}"
            f"{_num(r['next_day_close_pct']):>11.1f}  {why}")

    keep = [r for r in rows if not classify(r)]
    floored = [r for r in rows if (_num(r.get("filing_day_close")) or 0) >= args.price_floor]

    log(f"\n{args.target.upper()} under each treatment:")
    line("as published (everything)", summarise(rows, args.target))
    line("flagged rows removed", summarise(keep, args.target))
    line(f"price floor >= ${args.price_floor:.2f}", summarise(floored, args.target))

    pub = summarise(rows, args.target)
    cln = summarise(keep, args.target)
    if pub and cln:
        log(f"\n  {len(flagged)} rows out of {len(rows)} move the headline mean by "
            f"{pub['mean'] - cln['mean']:+.3f} percentage points.")
        if pub["mean"] > 0 >= cln["mean"]:
            log("  The published mean is POSITIVE only because of them. Cleaned, "
                "this list loses money.")

    log("\n  Note: avg_realistic_pnl_net_pct was already negative in the published")
    log("  summary for every list. That figure was honest and is unaffected here —")
    log("  a +2%/-1.5% bracket caps the artifact away, which is why it never")
    log("  showed the fake gain the mean did.")

    if args.write_clean:
        out = repo / "outcomes_clean.csv"
        tmp = out.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(allrows[0]))
            w.writeheader()
            w.writerows([r for r in allrows if not classify(r)])
        tmp.replace(out)
        log(f"\nwrote {out} ({len(allrows) - sum(1 for r in allrows if classify(r))} rows kept)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
