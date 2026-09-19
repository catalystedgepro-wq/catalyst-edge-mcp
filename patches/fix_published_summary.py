#!/usr/bin/env python3
"""fix_published_summary.py — correct a live outcomes_summary.csv now.

The pipeline patch (0001) fixes future runs. This corrects the file already
being served, without waiting for the next run.

  python3 fix_published_summary.py --outcomes /opt/catalyst/outcomes.csv \
                                   --summary  /opt/catalyst/outcomes_summary.csv
  # add --write once the before/after table looks right

Adds avg_alpha_close_pct_investable, median_alpha_close_pct, investable_rows
and excluded_rows. Leaves avg_alpha_close_pct untouched: silently restating a
published number is worse than correcting it in the open, and downstream
consumers read that column.

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

PRICE_FLOOR = 1.00
MAX_PLAUSIBLE_MOVE = 100.0
NEW_COLS = ["avg_alpha_close_pct_investable", "median_alpha_close_pct",
            "investable_rows", "excluded_rows"]


def f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outcomes", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--write", action="store_true",
                    help="write the corrected summary; otherwise dry run")
    args = ap.parse_args(argv)

    for p in (args.outcomes, args.summary):
        if not p.exists():
            print(f"not found: {p}")
            return 2

    with args.outcomes.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with args.summary.open(newline="", encoding="utf-8") as fh:
        summary = list(csv.DictReader(fh))
        fields = list(summary[0]) if summary else []

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("list_name", ""), []).append(r)

    print(f"{'list':24}{'published':>12}{'investable':>12}{'median':>10}"
          f"{'excluded':>10}{'of':>8}")
    changed = 0
    for g in summary:
        grp = groups.get(g["list_name"], [])
        if not grp:
            continue
        alphas = [f(x.get("alpha_close_pct")) for x in grp]
        inv = [x for x in grp
               if f(x.get("filing_day_close")) >= PRICE_FLOOR
               and abs(f(x.get("next_day_close_pct"))) <= MAX_PLAUSIBLE_MOVE]
        inv_alphas = [f(x.get("alpha_close_pct")) for x in inv]
        g["avg_alpha_close_pct_investable"] = (
            f"{statistics.fmean(inv_alphas):.4f}" if inv_alphas else "0")
        g["median_alpha_close_pct"] = (
            f"{statistics.median(alphas):.4f}" if alphas else "0")
        g["investable_rows"] = str(len(inv))
        g["excluded_rows"] = str(len(grp) - len(inv))
        pub, new = f(g["avg_alpha_close_pct"]), f(g["avg_alpha_close_pct_investable"])
        flag = "  <-- sign flips" if (pub > 0) != (new > 0) else ""
        print(f"{g['list_name']:24}{pub:>12.4f}{new:>12.4f}"
              f"{f(g['median_alpha_close_pct']):>10.4f}"
              f"{g['excluded_rows']:>10}{len(grp):>8}{flag}")
        changed += 1

    if not args.write:
        print(f"\ndry run — {changed} row(s) would be updated. Re-run with --write.")
        return 0

    out_fields = fields + [c for c in NEW_COLS if c not in fields]
    tmp = args.summary.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=out_fields)
        w.writeheader()
        for g in summary:
            w.writerow({k: g.get(k, "") for k in out_fields})
    tmp.replace(args.summary)
    print(f"\nwrote {args.summary} ({changed} rows, {len(NEW_COLS)} columns added)")
    print("avg_alpha_close_pct is unchanged — point the site at "
          "avg_alpha_close_pct_investable (patch 0002).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
