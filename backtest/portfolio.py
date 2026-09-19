#!/usr/bin/env python3
"""portfolio.py — what the board would have returned under different rules.

Takes the daily board as a basket and asks what excluding, or inverting, the
short / Reg SHO cohort would have done to it.

  python3 -m backtest.portfolio --data-repo ../sec-catalyst-data
  python3 -m backtest.portfolio --data-repo ../sec-catalyst-data --top 25

Each pick date forms an equal-weight basket; the day's return is the mean of
its members. Variants are compared to the baseline PAIRED BY DAY, so the two
always face the same tape and no day-mix effect can leak in. The test on
those paired daily differences is Wilcoxon signed-rank — the returns are too
skewed for a mean-based test to mean much.

WHAT THE SHORT LEG DOES NOT INCLUDE
Reg SHO threshold listing is, by construction, a published marker of
persistent delivery failures — which is to say a hard-to-borrow marker. In
this ledger `exec_cost_pct` is identical for the cohort and everything else
(94.7% vs 94.5% at 0.2%), so the cost model prices no borrow at all. Every
inverted number below is therefore an UPPER BOUND that a real short would not
achieve, and for the Reg SHO names specifically the borrow may not exist at
any price. --borrow-bps applies a flat haircut so you can see how fast the
edge dies.

Stdlib only.
"""

from __future__ import annotations

import argparse
import math
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.evaluate import _num, find_ledger, load_ledger  # noqa: E402
from backtest.cuts import join_boards, split_signals  # noqa: E402

# One effect wearing seven labels: regsho and auto_regsho_threshold are
# identical sets, finra_regsho and finra_short overlap 95%.
SHORT_FAMILY = {"finra_short", "regsho", "auto_regsho_threshold",
                "finra_regsho", "ftd", "gap", "pill"}


def log(msg: str = "") -> None:
    print(msg)


def in_family(r: dict, family: set[str]) -> bool:
    return bool(family & set(split_signals(r.get("signals_fired", ""))))


def wilcoxon(diffs: list[float]) -> tuple[float, int]:
    """Signed-rank z and the number of non-zero pairs. Normal approximation."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n < 6:
        return 0.0, n
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    mu = n * (n + 1) / 4.0
    sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24.0)
    return ((w_plus - mu) / sigma if sigma else 0.0), n


def leg_return(r: dict, target: str, short: bool, borrow_bps: float) -> float | None:
    """One position's return, net of the ledger's own execution cost.

    A short also pays borrow, which the ledger does not model — hence
    borrow_bps, applied only to short legs.
    """
    v = _num(r.get(target))
    if v is None:
        return None
    cost = _num(r.get("exec_cost_pct")) or 0.0
    if short:
        return -v - cost - borrow_bps / 100.0
    return v - cost


def daily_series(rows: list[dict], target: str, family: set[str],
                 top: int | None, borrow_bps: float) -> dict[str, dict]:
    """Per-day equal-weight return for each strategy."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r.get("list_date", ""), []).append(r)

    out: dict[str, dict] = {}
    for day, picks in sorted(by_day.items()):
        if top:
            picks = sorted(picks, key=lambda r: -(_num(r.get("convergence_score"))
                                                  or _num(r.get("base_score")) or 0))[:top]
        fam = [r for r in picks if in_family(r, family)]
        rest = [r for r in picks if not in_family(r, family)]

        def avg(items, short=False):
            vals = [leg_return(r, target, short, borrow_bps) for r in items]
            vals = [v for v in vals if v is not None]
            return (sum(vals) / len(vals)) if vals else None

        base = avg(picks)
        excl = avg(rest)
        long_leg, short_leg = avg(rest), avg(fam, short=True)
        if base is None:
            continue
        # Inverted: long everything else, short the cohort, weighted by how
        # many names each leg actually has that day.
        inv = None
        if long_leg is not None and short_leg is not None and picks:
            w = len(fam) / len(picks)
            inv = long_leg * (1 - w) + short_leg * w
        out[day] = {"n": len(picks), "n_family": len(fam),
                    "baseline": base, "excluded": excl,
                    "inverted": inv, "short_only": avg(fam, short=True)}
    return out


def summarise(series: dict[str, dict], key: str) -> dict | None:
    vals = [(d, v[key]) for d, v in series.items() if v.get(key) is not None]
    if len(vals) < 5:
        return None
    r = [v for _, v in vals]
    cum = 1.0
    for x in r:
        cum *= (1 + x / 100.0)
    return {"days": len(r), "mean_daily": st.mean(r), "median_daily": st.median(r),
            "win_days": sum(1 for x in r if x > 0) / len(r),
            "compounded": (cum - 1) * 100.0,
            "worst": min(r), "best": max(r)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--target", default="alpha_close_pct")
    ap.add_argument("--top", type=int, help="take only the top N by score each day")
    ap.add_argument("--borrow-bps", type=float, default=0.0,
                    help="flat borrow cost in bps applied to short legs only")
    args = ap.parse_args(argv)

    repo = Path(args.data_repo).expanduser().resolve()
    rows = load_ledger(find_ledger(repo))
    join_boards(rows, repo, ["signals_fired", "convergence_score"])
    rows = [r for r in rows if r.get("signals_fired")]
    if not rows:
        log("no picks carry signals_fired after the join")
        return 1

    series = daily_series(rows, args.target, SHORT_FAMILY, args.top, args.borrow_bps)
    if not series:
        log("no usable days")
        return 1

    n_fam = sum(v["n_family"] for v in series.values())
    n_all = sum(v["n"] for v in series.values())
    log(f"{n_all} positions over {len(series)} days · "
        f"{n_fam} in the short/RegSHO cohort ({n_fam/n_all:.1%})")
    log(f"target {args.target}, net of the ledger's exec_cost_pct"
        + (f", plus {args.borrow_bps:.0f}bps borrow on shorts" if args.borrow_bps
           else ", NO borrow cost on shorts"))
    if args.top:
        log(f"top {args.top} by score each day")

    log(f"\n{'strategy':<22}{'days':>6}{'mean/day':>10}{'med/day':>10}"
        f"{'win days':>10}{'compounded':>12}{'worst day':>11}")
    rowspec = [("baseline (long all)", "baseline"),
               ("exclude cohort", "excluded"),
               ("invert cohort", "inverted"),
               ("short cohort only", "short_only")]
    summaries = {}
    for label, key in rowspec:
        s = summarise(series, key)
        if not s:
            continue
        summaries[key] = s
        log(f"{label:<22}{s['days']:>6}{s['mean_daily']:>10.3f}"
            f"{s['median_daily']:>10.3f}{s['win_days']:>9.0%}"
            f"{s['compounded']:>11.1f}%{s['worst']:>11.2f}")

    log(f"\n{'='*78}\nPAIRED AGAINST BASELINE, DAY BY DAY\n{'='*78}")
    for label, key in rowspec[1:]:
        diffs = [v[key] - v["baseline"] for v in series.values()
                 if v.get(key) is not None and v.get("baseline") is not None]
        if len(diffs) < 6:
            continue
        z, n = wilcoxon(diffs)
        med = st.median(diffs)
        verdict = "" if abs(z) > 1.96 else "   (not distinguishable from noise)"
        log(f"  {label:<22} median daily diff {med:+.3f}%  "
            f"z={z:+.2f} over {n} days{verdict}")

    # A short's risk is the right tail of what it is short. Report it, always.
    tail = sorted(v for v in (_num(r.get(args.target)) for r in rows
                              if in_family(r, SHORT_FAMILY)) if v is not None)
    if tail:
        log(f"\n{'='*78}\nSHORT-SIDE TAIL RISK\n{'='*78}")
        n = len(tail)
        big = [x for x in tail if x > 50]
        log(f"  cohort positions in scope: {n}")
        log(f"  median {tail[n//2]:+.2f}%  p90 {tail[int(.9*n)]:+.2f}%  "
            f"p99 {tail[int(.99*n)]:+.2f}%  max {tail[-1]:+.2f}%")
        log(f"  positions up >50%: {len(big)}   >100%: "
            f"{sum(1 for x in tail if x > 100)}")
        if tail[-1] > 50:
            per_day = n / max(1, len(series))
            w = 1.0 / per_day if per_day else 0
            log(f"  one {tail[-1]:+.0f}% name at {w:.1%} weight costs a short book "
                f"{-tail[-1]*w:.1f}% in a single day")
        log("  A short is unbounded on the upside. The median says this cohort")
        log("  drifts down; the maximum says one name can erase a year of that.")
        log("  Absence of a squeeze in a short window is not evidence of safety.")

    log(f"\n{'='*78}\nWHAT THIS DOES NOT PRICE\n{'='*78}")
    log("  The short legs above assume the borrow exists and is free. Reg SHO")
    log("  threshold listing is a published marker of persistent delivery")
    log("  failures, which is the definition of hard-to-borrow, and this")
    log("  ledger's exec_cost_pct does not distinguish the cohort at all.")
    log("  Re-run with --borrow-bps to see how much of the edge survives a")
    log("  realistic borrow. 'Exclude cohort' needs no borrow and is the")
    log("  change you can actually make.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
