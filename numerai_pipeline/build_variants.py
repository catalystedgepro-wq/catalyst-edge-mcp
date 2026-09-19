#!/usr/bin/env python3
"""build_variants.py — Numerai Signals submissions that isolate one component.

The live model `catalystedge` submits the percentile rank of
convergence_score PLUS a +/-0.10 DCF grade tilt, and scores +0.00954
(t=+7.20 raw, ~+3.6 after correcting for 20-day window overlap). That
validates the COMBINATION. It does not say which part earns it.

Numerai allows multiple models and each submission is free, so the cleanest
way to find out is to run the variants alongside the control for a quarter.

  python3 -m numerai_pipeline.build_variants --variant nodcf
  python3 -m numerai_pipeline.build_variants --variant noshort
  python3 -m numerai_pipeline.build_variants --variant control   # reproduces live

  control   rank(convergence_score) + DCF tilt      what runs today
  nodcf     rank(convergence_score), no tilt        is DCF carrying it?
  noshort   rank(score minus short/RegSHO points)   does removing that weight
                                                    help, hurt, or do nothing?

`noshort` is the important one. On raw next-day outcomes that weight looked
harmful and I recommended zeroing it. Numerai then showed the score works at
a 20-day neutralised horizon, where I have no evidence either way. Submitting
it as a separate model tests the change for free instead of shipping it blind
into a scorer that is currently earning.

Stdlib only. Writes numerai_signals_<variant>.csv in Numerai's format.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path

# The same point columns the scanner awards on its "squeeze fuel" thesis.
FAMILY_PTS = ["regsho_pts", "finra_short_pts", "finra_regsho_pts", "ftd_pts",
              "short_pts", "gap_pts", "pill_pts", "squeeze_pts"]
DCF_TILT = {"A": 0.10, "B": 0.05, "C": 0.0, "D": -0.05, "F": -0.10}


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def next_friday(today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    return today + dt.timedelta(days=(4 - today.weekday()) % 7)


def load_dcf(path: Path) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        out = {}
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip().upper()
            g = (r.get("dcf_grade") or r.get("grade") or "").strip().upper()[:1]
            if t and g:
                out[t] = g
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", required=True,
                    choices=["control", "nodcf", "noshort"])
    ap.add_argument("--convergence", type=Path,
                    default=Path("/opt/catalyst/convergence_alerts.csv"))
    ap.add_argument("--dcf", type=Path, default=Path("/opt/catalyst/sec_xbrl_dcf.csv"))
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if not args.convergence.exists():
        print(f"not found: {args.convergence}")
        return 2
    with args.convergence.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("convergence file is empty")
        return 2

    def score(r):
        s = num(r.get("convergence_score"))
        if args.variant == "noshort":
            s -= sum(num(r.get(c)) for c in FAMILY_PTS)
        return s

    ranked = sorted(((r.get("ticker") or "").strip().upper(), score(r))
                    for r in rows if (r.get("ticker") or "").strip())
    ranked.sort(key=lambda p: p[1])          # ascending: rank 0 = most bearish
    dcf = load_dcf(args.dcf) if args.variant in ("control",) else {}
    total = len(ranked)
    friday = next_friday().strftime("%Y%m%d")

    out = args.out or Path(f"numerai_signals_{args.variant}.csv")
    tilted = 0
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bloomberg_ticker", "signal", "friday_date"])
        for i, (ticker, _s) in enumerate(ranked):
            pct = i / max(1, total - 1)
            adj = DCF_TILT.get(dcf.get(ticker, ""), 0.0)
            if adj:
                tilted += 1
            w.writerow([f"{ticker} US", f"{max(0.001, min(0.999, pct + adj)):.4f}",
                        friday])

    print(f"variant       {args.variant}")
    print(f"rows          {total}")
    print(f"DCF tilt      {'applied to %d tickers' % tilted if tilted else 'none (by design)'}")
    if args.variant == "noshort":
        moved = sum(1 for r in rows if any(num(r.get(c)) for c in FAMILY_PTS))
        print(f"reranked      {moved} tickers had short/RegSHO points removed")
    print(f"wrote         {out.resolve()}")
    print(f"friday_date   {friday}")
    print("\nSubmit with numerapi against a SEPARATE model slug — never overwrite")
    print("the control, or the comparison is lost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
