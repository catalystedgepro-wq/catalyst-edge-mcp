#!/usr/bin/env python3
"""test_cuts.py — the subgroup analyser must resist the ways it can be fooled.

  python3 backtest/test_cuts.py

Subgroup analysis fails in three specific ways, and each has a test here
built from data whose right answer is known:

  1. a confound — the subgroup differs by WHEN it occurs, not what it is
  2. skew — a few huge winners move a mean the typical member never sees
  3. the search itself — enough subgroups and something always looks real

All three were hit for real on the published ledger while this was being
written, which is why each is pinned down here.

Stdlib only.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.cuts import (bonferroni_t, compare_by_date,  # noqa: E402
                           compare_rank_by_date)

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'ok ' if cond else '!! '} {msg}")
    if not cond:
        FAILURES.append(msg)


def rows_from(spec):
    return [{"list_date": d, "alpha_close_pct": v, "flag": f} for d, v, f in spec]


def test_confound() -> None:
    """The subgroup only exists on days that happened to be good.

    This is the ELEVATED conviction case from the real ledger: the label was
    introduced part-way through the period, so an unblocked comparison
    measured the calendar.

    The property under test is a RATE, not one draw. A single run at the 5%
    bar is allowed to exceed |t|=2 about 5% of the time — that is what the
    bar means — so asserting on one seed would itself fail one run in twenty.
    What must hold is that blocking takes the rejection rate from ~always
    down to ~nominal.
    """
    print("confounding by date")
    TRIALS = 40
    fake_gaps, blocked_hits, ranked_hits = 0, 0, 0
    for seed in range(TRIALS):
        random.seed(seed)
        spec = []
        for d in range(20):
            day = f"2026-08-{d+1:02d}"
            shift = 6.0 if d >= 10 else 0.0       # the tape changed halfway
            for i in range(40):
                # the flag only ever appears in the second half, and within a
                # day it carries no information whatsoever
                flag = d >= 10 and i < 20
                spec.append((day, random.gauss(0, 1) + shift, flag))
        rows = rows_from(spec)

        ins = [r["alpha_close_pct"] for r in rows if r["flag"]]
        outs = [r["alpha_close_pct"] for r in rows if not r["flag"]]
        if sum(ins) / len(ins) - sum(outs) / len(outs) > 2.0:
            fake_gaps += 1
        b = compare_by_date(rows, "alpha_close_pct", lambda r: r["flag"])
        if b and abs(b["t"]) > 2:
            blocked_hits += 1
        rk = compare_rank_by_date(rows, "alpha_close_pct", lambda r: r["flag"])
        if rk and abs(rk["t"]) > 2:
            ranked_hits += 1

    print(f"    unblocked shows a large fake gap in {fake_gaps}/{TRIALS} runs")
    print(f"    date-blocked exceeds |t|=2 in {blocked_hits}/{TRIALS} "
          f"· rank version {ranked_hits}/{TRIALS}  (nominal ~2/40)")
    check(fake_gaps == TRIALS, "unblocked comparison is fooled every time")
    check(blocked_hits <= TRIALS * 0.20,
          f"date-blocking drops the rate to near nominal ({blocked_hits}/{TRIALS})")
    check(ranked_hits <= TRIALS * 0.20,
          f"rank version likewise ({ranked_hits}/{TRIALS})")


def test_real_effect() -> None:
    """A genuine within-day difference must still be found."""
    print("genuine within-day effect")
    random.seed(2)
    spec = []
    for d in range(20):
        day = f"2026-08-{d+1:02d}"
        shift = random.gauss(0, 5)            # each day has its own level
        for i in range(40):
            flag = i < 20
            spec.append((day, random.gauss(0, 1) + shift - (1.5 if flag else 0), flag))
    rows = rows_from(spec)
    r = compare_rank_by_date(rows, "alpha_close_pct", lambda x: x["flag"])
    check(r is not None and r["t"] < -2,
          f"finds a real within-day effect through heavy day-to-day noise "
          f"(z={r['t'] if r else None})")


def test_skew() -> None:
    """Means say the subgroup wins; every typical member loses.

    The real ledger's alpha_close_pct has mean +4.3% and median -0.3% in the
    same cohort. A mean-based test reads the tail; the rank test reads the
    typical pick.
    """
    print("right-skew")
    random.seed(3)
    spec = []
    for d in range(20):
        day = f"2026-08-{d+1:02d}"
        for i in range(40):
            flag = i < 20
            if flag:
                # slightly worse nearly always, with a rare enormous winner
                v = -0.4 + (200.0 if random.random() < 0.02 else 0.0)
            else:
                v = 0.0 + random.gauss(0, 0.5)
            spec.append((day, v, flag))
    rows = rows_from(spec)
    mean_based = compare_by_date(rows, "alpha_close_pct", lambda r: r["flag"])
    ranked = compare_rank_by_date(rows, "alpha_close_pct", lambda r: r["flag"])
    check(mean_based["delta"] > 0,
          f"the mean says the subgroup is BETTER ({mean_based['delta']:+.2f}%)")
    check(ranked["delta"] < 0,
          f"the median says it is worse ({ranked['delta']:+.2f}%)")
    check(ranked["t"] < -2,
          f"the rank test detects the real direction (z={ranked['t']:+.2f})")


def test_search_burden() -> None:
    print("multiple comparisons")
    check(abs(bonferroni_t(1) - 1.96) < 0.01, "one test keeps the 1.96 bar")
    check(3.2 < bonferroni_t(49) < 3.5, f"49 tests -> {bonferroni_t(49):.2f}")
    check(bonferroni_t(900) > bonferroni_t(49), "the bar rises with the search")


def test_thin_groups() -> None:
    print("guards")
    rows = rows_from([(f"2026-08-{d+1:02d}", float(i), i < 2)
                      for d in range(3) for i in range(10)])
    check(compare_rank_by_date(rows, "alpha_close_pct", lambda r: r["flag"]) is None,
          "refuses a subgroup too small to test")
    check(compare_rank_by_date([], "alpha_close_pct", lambda r: True) is None,
          "handles an empty input")


def main() -> int:
    test_confound()
    test_real_effect()
    test_skew()
    test_search_burden()
    test_thin_groups()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
