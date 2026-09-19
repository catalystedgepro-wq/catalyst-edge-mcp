#!/usr/bin/env python3
"""test_portfolio.py — the simulator must not flatter a short.

  python3 backtest/test_portfolio.py

Stdlib only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.portfolio import (daily_series, in_family, leg_return,  # noqa: E402
                                summarise, wilcoxon)

FAILURES: list[str] = []
FAM = {"regsho"}


def check(cond: bool, msg: str) -> None:
    print(f"  {'ok ' if cond else '!! '} {msg}")
    if not cond:
        FAILURES.append(msg)


def mk(day, tick, ret, flagged, cost=0.2):
    return {"list_date": day, "ticker": tick, "alpha_close_pct": ret,
            "exec_cost_pct": cost, "convergence_score": 10,
            "signals_fired": "regsho" if flagged else "other"}


def main() -> int:
    print("costs and direction")
    long_r = leg_return(mk("d", "A", 5.0, False), "alpha_close_pct", False, 0)
    short_r = leg_return(mk("d", "A", 5.0, True), "alpha_close_pct", True, 0)
    check(abs(long_r - 4.8) < 1e-9, "a long nets the execution cost (5.0 -> 4.8)")
    check(abs(short_r - -5.2) < 1e-9, "a short inverts AND pays cost (5.0 -> -5.2)")
    borrowed = leg_return(mk("d", "A", 5.0, True), "alpha_close_pct", True, 50)
    check(abs(borrowed - -5.7) < 1e-9, "borrow applies to shorts only (50bps -> -5.7)")
    check(leg_return(mk("d", "A", 5.0, False), "alpha_close_pct", False, 50) == long_r,
          "borrow never touches a long")

    print("a short is destroyed by the right tail")
    # The cohort drifts down on almost every name, then one squeezes.
    rows = []
    for d in range(10):
        day = f"2026-08-{d+1:02d}"
        for i in range(10):
            rows.append(mk(day, f"F{i}", -1.0, True))
            rows.append(mk(day, f"N{i}", 0.0, False))
    rows.append(mk("2026-08-11", "BOOM", 3000.0, True))
    for i in range(9):
        rows.append(mk("2026-08-11", f"F{i}", -1.0, True))
        rows.append(mk("2026-08-11", f"N{i}", 0.0, False))
    series = daily_series(rows, "alpha_close_pct", FAM, None, 0)
    short = summarise(series, "short_only")
    check(short["median_daily"] > 0, "the median short day is a winner")
    check(short["compounded"] < 0,
          f"yet compounding is negative ({short['compounded']:.1f}%) — one "
          f"squeeze outweighs every quiet day")
    check(short["win_days"] >= 0.9, f"win rate looks great ({short['win_days']:.0%}) "
          f"while the strategy loses money")

    print("exclusion needs no borrow")
    s2 = daily_series(rows, "alpha_close_pct", FAM, 0 or None, 999)
    check(summarise(s2, "excluded")["mean_daily"]
          == summarise(series, "excluded")["mean_daily"],
          "an enormous borrow cost cannot change the exclude-only variant")

    print("paired test")
    z, n = wilcoxon([0.5] * 20)
    check(z > 3 and n == 20, "detects a consistent one-sided difference")
    z2, _ = wilcoxon([0.5, -0.5] * 10)
    check(abs(z2) < 1.0, "reports nothing for a symmetric difference")
    check(wilcoxon([1.0, 2.0])[0] == 0.0, "refuses too few pairs")

    print("family membership")
    check(in_family(mk("d", "A", 0, True), FAM), "matches a fired signal")
    check(not in_family(mk("d", "A", 0, False), FAM), "does not match otherwise")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
