#!/usr/bin/env python3
"""test_backtest.py — cover the ledger evaluator on fixtures with known answers.

  python3 backtest/test_backtest.py

The point is not that it runs. A statistics tool that runs but reports edge
where there is none is worse than no tool, so every check here builds a
fixture whose correct answer is known in advance and requires the evaluator
to reach it — including the cases designed to fool it.

Stdlib only.
"""

from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FAILURES: list[str] = []

LEDGER_COLS = ["list_name", "list_date", "ticker", "base_score",
               "alpha_close_pct", "gap_next_open_pct", "next_day_vwap_pct",
               "realistic_pnl_net_pct"]


def check(cond: bool, msg: str) -> None:
    print(f"  {'ok ' if cond else '!! '} {msg}")
    if not cond:
        FAILURES.append(msg)


def build_repo(root: Path, mode: str, n_days: int = 40, per_day: int = 40) -> int:
    """A ledger shaped like the published one. `mode` sets the truth:
    'signal'  base_score predicts the outcome
    'noise'   base_score is independent of it
    """
    random.seed(11)
    snap = root / "snapshots" / "2026-09-18"
    snap.mkdir(parents=True)
    rows, boards = [], {}
    for d in range(n_days):
        day = f"2026-08-{d+1:02d}" if d < 31 else f"2026-09-{d-30:02d}"
        board = []
        for i in range(per_day):
            fwd = random.gauss(0, 4)
            score = (50 + fwd * 2 + random.gauss(0, 3) if mode == "signal"
                     else random.uniform(0, 100))
            t = f"T{i:03d}"
            rows.append({"list_name": "sec_top_gappers", "list_date": day,
                         "ticker": t, "base_score": round(score, 2),
                         "alpha_close_pct": round(fwd, 4),
                         "gap_next_open_pct": round(fwd * .6, 4),
                         "next_day_vwap_pct": round(fwd * .8, 4),
                         "realistic_pnl_net_pct": round(2.0 if fwd > 2 else -1.7, 4)})
            # An independent overlay, for the join path.
            board.append({"ticker": t, "convergence_score": round(random.uniform(-50, 50), 2)})
        boards[day] = board
    with (snap / "outcomes.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
        w.writeheader()
        w.writerows(rows)
    for day, board in boards.items():
        d = root / "snapshots" / day
        d.mkdir(parents=True, exist_ok=True)
        with (d / "convergence_alerts.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ticker", "convergence_score"])
            w.writeheader()
            w.writerows(board)
    return len(rows)


def run(root: Path, *extra: str) -> dict:
    r = subprocess.run([sys.executable, "-m", "backtest.evaluate",
                        "--data-repo", str(root), "--json", *extra],
                       capture_output=True, text=True, cwd=REPO, timeout=180)
    if "{" not in r.stdout:
        print(r.stdout[-400:], r.stderr[-400:])
        return {}
    return json.loads(r.stdout[r.stdout.index("{"):])


def main() -> int:
    print("ledger evaluator")
    sig = Path(tempfile.mkdtemp())
    n = build_repo(sig, "signal")
    rep = run(sig)
    res = rep.get("per_list", {}).get("sec_top_gappers", [])
    alpha = next((a for a in res if a["target"] == "alpha_close_pct"), None)
    check(rep.get("picks") == n, f"loads every pick ({n})")
    check(alpha is not None and alpha["spearman"] > 0.4, "detects a planted signal")
    check(alpha is not None and abs(alpha["q5_minus_q1_t"] or 0) > 2,
          "planted signal clears |t| = 2")

    noise = Path(tempfile.mkdtemp())
    build_repo(noise, "noise")
    rep_n = run(noise)
    res_n = rep_n.get("per_list", {}).get("sec_top_gappers", [])
    alpha_n = next((a for a in res_n if a["target"] == "alpha_close_pct"), None)
    check(alpha_n is not None and abs(alpha_n["spearman"]) < 0.1,
          "reports no correlation for a pure-noise predictor")
    check(alpha_n is not None and abs(alpha_n["q5_minus_q1_t"] or 0) < 2,
          "noise spread stays inside |t| = 2")

    print("join path")
    rep_j = run(sig, "--join-predictor", "convergence_score")
    joined = rep_j.get("per_list", {}).get("sec_top_gappers", [])
    ja = next((a for a in joined if a["target"] == "alpha_close_pct"), None)
    check(rep_j.get("predictor") == "convergence_score", "join switches the predictor")
    check(ja is not None and ja["n"] > 0, "join matches on (list_date, ticker)")
    check(ja is not None and abs(ja["spearman"]) < 0.1,
          "an independent overlay shows no correlation")

    print("guards")
    check(rep.get("multiple_comparisons", {}).get("tests", 0) > 0,
          "counts how many hypothesis tests were run")

    from backtest.evaluate import list_containment
    groups = {"big": [{"list_date": "d", "ticker": f"T{i}"} for i in range(100)],
              "small": [{"list_date": "d", "ticker": f"T{i}"} for i in range(10)],
              "other": [{"list_date": "d", "ticker": f"X{i}"} for i in range(10)]}
    cont = dict(list_containment(groups))
    check(cont["small"] == ["big"], "detects a nested list")
    check(cont["other"] == [], "does not call a disjoint list nested")
    check(cont["big"] == [], "the universe is not a subset of anything")

    print("failure modes")
    empty = Path(tempfile.mkdtemp())
    r = subprocess.run([sys.executable, "-m", "backtest.evaluate",
                        "--data-repo", str(empty)],
                       capture_output=True, text=True, cwd=REPO, timeout=60)
    check(r.returncode == 2 and "outcome-ledger" in r.stdout,
          "missing ledger gives guidance, not a traceback")
    r2 = subprocess.run([sys.executable, "-m", "backtest.evaluate",
                         "--data-repo", str(sig), "--targets", "made_up_column"],
                        capture_output=True, text=True, cwd=REPO, timeout=60)
    check(r2.returncode == 2 and "unknown target" in r2.stdout,
          "rejects an unknown target column")

    from backtest.evaluate import assess_incremental
    print("incremental analysis")
    import random as _r
    _r.seed(5)

    def build(mode, count=2000):
        out = []
        for _ in range(count):
            a, b = _r.gauss(0, 1), _r.gauss(0, 1)
            fwd = 2.0 * a + 2.0 * b + _r.gauss(0, 1.5)
            base = 50 + a * 10 + _r.gauss(0, 2)
            overlay = {"noise": _r.random(), "duplicate": base * 3.0 - 7.0,
                       "2nd_look": base + _r.gauss(0, 3),
                       "additive": b + _r.gauss(0, .15)}[mode]
            out.append({"convergence_score": base, "kronos_p_up": overlay,
                        "fwd_return_pct": fwd})
        return out

    inc = {m: assess_incremental(build(m))
           for m in ("noise", "duplicate", "2nd_look", "additive")}
    for m, v in inc.items():
        print(f"    {m:10} incr={v['incremental_pct']:+7.3f}%  t={v['t_stat']:+6.2f}")
    check(abs(inc["noise"]["t_stat"]) < 2, "rejects a pure-noise overlay")
    check(abs(inc["duplicate"]["t_stat"]) < 2,
          "rejects an overlay that is a function of the base")
    check(inc["additive"]["t_stat"] > 2, "credits an overlay with a new driver")
    check(inc["additive"]["t_stat"] > inc["2nd_look"]["t_stat"],
          "ranks new information above a re-measurement")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
