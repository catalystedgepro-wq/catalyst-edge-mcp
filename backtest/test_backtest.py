#!/usr/bin/env python3
"""test_backtest.py — cover the ledger and evaluator on synthetic data.

  python3 backtest/test_backtest.py

The point of the evaluator test is not that it runs. A statistics tool that
runs but reports edge where there is none is worse than no tool. So this
plants a known signal in one predictor and pure noise in another, and
requires the evaluator to find the first and reject the second.

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
TICKERS = [f"T{i:02d}" for i in range(30)]
NBARS, HORIZON, NDAYS = 200, 5, 40


def check(cond: bool, msg: str) -> None:
    print(f"  {'ok ' if cond else '!! '} {msg}")
    if not cond:
        FAILURES.append(msg)


def build_fixture(root: Path) -> int:
    random.seed(11)
    (root / "ohlcv").mkdir(parents=True)
    bars = {}
    for t in TICKERS:
        px, rows = 100.0, []
        for d in range(NBARS):
            px *= (1 + random.gauss(0, 0.015))
            day = (f"2026-01-{d+1:02d}" if d < 31
                   else f"2026-{2+(d-31)//30:02d}-{((d-31)%30)+1:02d}")
            rows.append({"date": day, "open": round(px * .999, 4),
                         "high": round(px * 1.01, 4), "low": round(px * .99, 4),
                         "close": round(px, 4), "volume": 1e6})
        rows.sort(key=lambda r: r["date"])
        bars[t] = rows
        with (root / f"ohlcv/{t}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low",
                                              "close", "volume"])
            w.writeheader()
            w.writerows(rows)

    def realized(t, pick_date):
        b = bars[t]
        i = next((k for k, x in enumerate(b) if x["date"] >= pick_date), None)
        if i is None or i + HORIZON >= len(b):
            return None
        return (b[i + HORIZON]["close"] - b[i]["open"]) / b[i]["open"] * 100

    (root / "snapshots/convergence").mkdir(parents=True)
    (root / "snapshots/kronos").mkdir(parents=True)
    n = 0
    for day in [b["date"] for b in bars["T00"][20:20 + NDAYS]]:
        conv, kron = [], []
        for t in TICKERS:
            r = realized(t, day)
            if r is None:
                continue
            n += 1
            # Signal: score tracks the true forward move, plus noise.
            score = 50 + r * 3 + random.gauss(0, 6)
            conv.append({"ticker": t, "convergence_score": round(score, 2),
                         "conviction_level": "HIGH" if score > 55 else "MEDIUM"})
            # Noise: p_up is independent of the outcome by construction.
            kron.append({"ticker": t, "p_up": round(random.random(), 4),
                         "pred_return_pct": round(random.gauss(0, 2), 4)})
        for sub, rows, cols in (
                ("convergence", conv, ["ticker", "convergence_score", "conviction_level"]),
                ("kronos", kron, ["ticker", "p_up", "pred_return_pct"])):
            with (root / f"snapshots/{sub}/{day}.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
    return n


def test_incremental() -> None:
    """The overlay must be credited only for information the base lacks.

    Four synthetic overlays over a base that sees latent driver `a`:
      noise      independent of everything      -> reject
      duplicate  a pure function of the base    -> reject (the trap: an
                 unstratified split would credit the base's own skill here)
      2nd_look   an independent noisy re-read of `a` -> weak credit; a second
                 measurement genuinely sharpens the estimate
      additive   sees a second driver `b`       -> strong credit
    """
    print("incremental analysis")
    import random as _r
    from backtest.evaluate import assess_incremental
    _r.seed(5)

    def build(mode, n=2000):
        rows = []
        for _ in range(n):
            a, b = _r.gauss(0, 1), _r.gauss(0, 1)
            fwd = 2.0 * a + 2.0 * b + _r.gauss(0, 1.5)
            conv = 50 + a * 10 + _r.gauss(0, 2)
            overlay = {"noise": _r.random(),
                       "duplicate": conv * 3.0 - 7.0,
                       "2nd_look": conv + _r.gauss(0, 3),
                       "additive": b + _r.gauss(0, .15)}[mode]
            rows.append({"convergence_score": conv, "kronos_p_up": overlay,
                         "fwd_return_pct": fwd})
        return rows

    res = {m: assess_incremental(build(m))
           for m in ("noise", "duplicate", "2nd_look", "additive")}
    for m, r in res.items():
        print(f"    {m:10} incr={r['incremental_pct']:+7.3f}%  t={r['t_stat']:+6.2f}")
    check(abs(res["noise"]["t_stat"]) < 2, "rejects a pure-noise overlay")
    check(abs(res["duplicate"]["t_stat"]) < 2,
          "rejects an overlay that is a function of the base")
    check(res["additive"]["t_stat"] > 2, "credits an overlay with a new driver")
    check(res["additive"]["t_stat"] > res["2nd_look"]["t_stat"],
          "ranks new information above a re-measurement")


def main() -> int:
    root = Path(tempfile.mkdtemp())
    planted = build_fixture(root)
    print(f"fixture: {planted} picks over {NDAYS} days, {len(TICKERS)} tickers")

    env = dict(os.environ, CATALYST_DATA_ROOT=str(root))
    r = subprocess.run([sys.executable, "-m", "backtest.evaluate",
                        "--horizon", str(HORIZON), "--json"],
                       capture_output=True, text=True, cwd=REPO, env=env, timeout=120)
    if r.returncode != 0 or "{" not in r.stdout:
        print(r.stdout[-500:], r.stderr[-500:])
        return 1
    rep = json.loads(r.stdout[r.stdout.index("{"):])
    by = {p["predictor"]: p for p in rep["predictors"]}

    conv = by["convergence_score"]["spearman_vs_fwd_return"]
    noise = by["kronos_p_up"]["spearman_vs_fwd_return"]
    print(f"  planted-signal spearman {conv:+.3f} | pure-noise spearman {noise:+.3f}")
    check(conv > 0.4, "detects a planted signal")
    check(abs(noise) < 0.15, "reports no edge for a pure-noise predictor")
    check(by["convergence_score"]["top_minus_bottom_pct"] > 0,
          "quantile buckets are monotone in the signal")
    check(rep["picks"] == planted and rep["distinct_days"] == NDAYS,
          "every planted pick is accounted for")

    ledger = root / "backtest_ledger.csv"
    check(ledger.exists(), "ledger written")
    if ledger.exists():
        import backtest.evaluate as ev
        rows = list(csv.DictReader(ledger.open()))
        check(list(rows[0]) == ev.LEDGER_FIELDS, "ledger header matches LEDGER_FIELDS")
        # Entry must be the pick date's OPEN, never a price the pick could see.
        check(all(r["entry_date"] >= r["pick_date"] for r in rows),
              "no lookahead: entry never precedes the pick date")
        check(all(r["exit_date"] > r["entry_date"] for r in rows),
              "exit always follows entry")

    # An empty data root must say so, not crash.
    empty = Path(tempfile.mkdtemp())
    r2 = subprocess.run([sys.executable, "-m", "backtest.evaluate"],
                        capture_output=True, text=True, cwd=REPO, timeout=60,
                        env=dict(os.environ, CATALYST_DATA_ROOT=str(empty)))
    check(r2.returncode == 1 and "run backtest.snapshot" in r2.stdout,
          "empty data root gives guidance, not a traceback")

    test_incremental()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
