#!/usr/bin/env python3
"""evaluate.py — build a per-pick ledger and compare predictors on it.

Joins the daily snapshots (backtest.snapshot) to the OHLCV cache
(kronos_pipeline.ohlcv) to compute what each pick actually did, then asks the
only question that matters: does Kronos add anything the convergence score
does not already capture?

  python3 -m backtest.evaluate --horizon 5
  python3 -m backtest.evaluate --horizon 5 --ledger-only   # just write the CSV

Writes $CATALYST_DATA_ROOT/backtest_ledger.csv — the per-pick outcome record
this pipeline never had. Its schema is defined here because this file creates
it; no existing file's schema is assumed anywhere.

ENTRY TIMING AND LOOKAHEAD
Picks are ranked before the open, from data through the prior close. So entry
is the OPEN of the pick date and exit is the CLOSE `horizon` sessions later.
Using the pick date's close as entry would let the pick see the move it is
being judged on. If your pipeline actually publishes intraday, this
assumption is wrong and --entry close is the honest setting instead.

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

DATA_ROOT = Path(os.environ.get("CATALYST_DATA_ROOT", "/opt/catalyst"))
SNAP_ROOT = DATA_ROOT / "snapshots"
OHLCV_DIR = DATA_ROOT / "ohlcv"
LEDGER = DATA_ROOT / "backtest_ledger.csv"

LEDGER_FIELDS = ["pick_date", "ticker", "convergence_score", "conviction_level",
                 "kronos_p_up", "kronos_pred_return_pct", "entry_date",
                 "entry_price", "exit_date", "exit_price", "fwd_return_pct",
                 "horizon"]

MIN_PICKS = 200   # below this, bucket statistics are noise


def log(msg: str = "") -> None:
    print(msg, file=sys.stdout)


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── data loading ─────────────────────────────────────────────────────────────

def load_bars(ticker: str) -> list[dict]:
    path = OHLCV_DIR / f"{ticker}.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_snapshots(subdir: str) -> dict[str, list[dict]]:
    d = SNAP_ROOT / subdir
    if not d.exists():
        return {}
    out = {}
    for p in sorted(d.glob("*.csv")):
        with p.open(newline="", encoding="utf-8") as f:
            out[p.stem] = list(csv.DictReader(f))
    return out


def forward_return(bars: list[dict], pick_date: str, horizon: int,
                   entry_at: str) -> tuple | None:
    """(entry_date, entry_px, exit_date, exit_px, pct) or None if unavailable."""
    idx = next((i for i, b in enumerate(bars) if b["date"] >= pick_date), None)
    if idx is None or idx + horizon >= len(bars):
        return None                      # pick predates the cache, or the
                                         # horizon has not completed yet
    entry_bar, exit_bar = bars[idx], bars[idx + horizon]
    entry = _num(entry_bar["open" if entry_at == "open" else "close"])
    exit_ = _num(exit_bar["close"])
    if not entry or not exit_ or entry <= 0:
        return None
    return (entry_bar["date"], entry, exit_bar["date"], exit_,
            (exit_ - entry) / entry * 100.0)


def build_ledger(horizon: int, entry_at: str) -> list[dict]:
    convergence = load_snapshots("convergence")
    kronos = load_snapshots("kronos")
    if not convergence:
        log(f"no snapshots under {SNAP_ROOT/'convergence'} — run backtest.snapshot daily first")
        return []

    bars_cache: dict[str, list[dict]] = {}
    rows, no_bars, incomplete = [], set(), 0

    for day, picks in convergence.items():
        kmap = {(r.get("ticker") or "").upper(): r
                for r in kronos.get(day, [])}
        for pick in picks:
            t = (pick.get("ticker") or "").strip().upper()
            if not t:
                continue
            if t not in bars_cache:
                bars_cache[t] = load_bars(t)
            bars = bars_cache[t]
            if not bars:
                no_bars.add(t)
                continue
            fwd = forward_return(bars, day, horizon, entry_at)
            if fwd is None:
                incomplete += 1
                continue
            k = kmap.get(t, {})
            rows.append({
                "pick_date": day,
                "ticker": t,
                "convergence_score": _num(pick.get("convergence_score"), ""),
                "conviction_level": pick.get("conviction_level", ""),
                "kronos_p_up": _num(k.get("p_up"), ""),
                "kronos_pred_return_pct": _num(k.get("pred_return_pct"), ""),
                "entry_date": fwd[0], "entry_price": round(fwd[1], 4),
                "exit_date": fwd[2], "exit_price": round(fwd[3], 4),
                "fwd_return_pct": round(fwd[4], 4),
                "horizon": horizon,
            })

    if no_bars:
        log(f"note: {len(no_bars)} ticker(s) had no OHLCV cache and were skipped "
            f"(e.g. {', '.join(sorted(no_bars)[:5])})")
    if incomplete:
        log(f"note: {incomplete} pick(s) skipped — horizon not yet complete")
    return rows


# ── statistics (stdlib) ──────────────────────────────────────────────────────

def _ranks(xs: list[float]) -> list[float]:
    """Average ranks, so ties do not bias the correlation."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation — monotone association, robust to the fat tails and
    outliers that make Pearson misleading on returns."""
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else None


def buckets(pairs: list[tuple[float, float]], n: int = 5) -> list[dict]:
    """Quantile buckets of the predictor, with realized return in each."""
    pairs = sorted(pairs, key=lambda p: p[0])
    if len(pairs) < n:
        return []
    size, out = len(pairs) // n, []
    for b in range(n):
        lo = b * size
        hi = len(pairs) if b == n - 1 else (b + 1) * size
        chunk = pairs[lo:hi]
        rets = [r for _, r in chunk]
        out.append({
            "bucket": b + 1,
            "n": len(chunk),
            "predictor_range": [round(chunk[0][0], 4), round(chunk[-1][0], 4)],
            "mean_return_pct": round(sum(rets) / len(rets), 4),
            "hit_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
        })
    return out


def assess(rows: list[dict], predictor: str) -> dict | None:
    pairs = [(float(r[predictor]), float(r["fwd_return_pct"]))
             for r in rows if r.get(predictor) not in ("", None)]
    if len(pairs) < 3:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    bs = buckets(pairs)
    return {
        "predictor": predictor,
        "n": len(pairs),
        "spearman_vs_fwd_return": round(spearman(xs, ys) or 0.0, 4),
        "buckets": bs,
        "top_minus_bottom_pct": (round(bs[-1]["mean_return_pct"]
                                       - bs[0]["mean_return_pct"], 4)
                                 if bs else None),
    }


def assess_incremental(rows: list[dict], base: str = "convergence_score",
                       overlay: str = "kronos_p_up", top_frac: float = 0.2,
                       strata: int = 4) -> dict | None:
    """Does the overlay add anything ON TOP of the base predictor?

    Comparing two predictors side by side cannot answer this. If Kronos merely
    re-derives what the convergence score already captures, both look good
    separately and the overlay is worth nothing.

    The naive version — take the base's top slice, split it by the overlay —
    has a trap. If the overlay is any monotone function of the base, that
    split is just the base splitting itself, and the base predicts returns, so
    the overlay gets credited for the base's work.

    So the comparison is STRATIFIED: the top slice is cut into bands of
    similar base score, the overlay split happens inside each band, and the
    per-band deltas are pooled. Within a narrow band the base is near-constant,
    so anything the overlay separates there is information the base does not
    carry. An overlay that is a function of the base has no independent
    variation inside a band and correctly scores zero.

    Significance is Welch's t on the pooled delta — a raw percentage gap means
    nothing without the noise it sits in.
    """
    usable = [r for r in rows
              if r.get(base) not in ("", None) and r.get(overlay) not in ("", None)]
    if len(usable) < 80:
        return None

    usable.sort(key=lambda r: float(r[base]), reverse=True)
    n_top = max(40, int(len(usable) * top_frac))
    top = usable[:n_top]

    def moments(chunk):
        rets = [float(r["fwd_return_pct"]) for r in chunk]
        n = len(rets)
        mean = sum(rets) / n
        var = sum((x - mean) ** 2 for x in rets) / (n - 1) if n > 1 else 0.0
        return n, mean, var

    band_size = max(20, len(top) // strata)
    lo_all, hi_all, num, var_sum, bands = [], [], 0.0, 0.0, 0

    for i in range(0, len(top), band_size):
        band = top[i:i + band_size]
        if len(band) < 20:            # a remainder too small to split
            break
        band = sorted(band, key=lambda r: float(r[overlay]))
        half = len(band) // 2
        lo, hi = band[:half], band[half:]
        n_lo, m_lo, v_lo = moments(lo)
        n_hi, m_hi, v_hi = moments(hi)
        w = len(band)
        num += (m_hi - m_lo) * w
        var_sum += (v_lo / n_lo + v_hi / n_hi) * w * w
        bands += w
        lo_all += lo
        hi_all += hi

    if not bands:
        return None
    delta = num / bands
    se = math.sqrt(var_sum) / bands
    t = delta / se if se > 0 else 0.0

    def summarise(chunk):
        n, mean, _ = moments(chunk)
        rets = [float(r["fwd_return_pct"]) for r in chunk]
        return {"n": n, "mean_return_pct": round(mean, 4),
                "hit_rate": round(sum(1 for x in rets if x > 0) / n, 4)}

    if abs(t) < 2.0:
        verdict = ("no separation beyond noise — the overlay adds nothing the "
                   "base does not already capture")
    elif t > 0:
        verdict = "overlay separates within bands of equal base score"
    else:
        verdict = "overlay separates INVERSELY — high overlay does worse"

    return {
        "base": base, "overlay": overlay,
        "base_top_slice": {"frac": top_frac, "n": len(top), "strata": bands // band_size},
        "overlay_low": summarise(lo_all), "overlay_high": summarise(hi_all),
        "incremental_pct": round(delta, 4),
        "t_stat": round(t, 3),
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, default=5, help="trading days held")
    ap.add_argument("--entry", choices=["open", "close"], default="open")
    ap.add_argument("--ledger-only", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    rows = build_ledger(args.horizon, args.entry)
    if not rows:
        log("no completed picks to evaluate")
        return 1

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(LEDGER)
    log(f"wrote {len(rows)} picks to {LEDGER}")
    if args.ledger_only:
        return 0

    days = len({r["pick_date"] for r in rows})
    report = {
        "picks": len(rows), "distinct_days": days,
        "horizon": args.horizon, "entry": args.entry,
        "mean_return_pct": round(sum(r["fwd_return_pct"] for r in rows) / len(rows), 4),
        "predictors": [a for a in (assess(rows, "convergence_score"),
                                   assess(rows, "kronos_p_up"),
                                   assess(rows, "kronos_pred_return_pct")) if a],
        "incremental": assess_incremental(rows),
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        log(f"\n{report['picks']} picks over {days} day(s), "
            f"{args.horizon}d horizon, entry at {args.entry}")
        log(f"baseline mean return: {report['mean_return_pct']:+.3f}%\n")
        for a in report["predictors"]:
            log(f"── {a['predictor']}  (n={a['n']}, "
                f"spearman={a['spearman_vs_fwd_return']:+.3f})")
            for b in a["buckets"]:
                log(f"     Q{b['bucket']}  {str(b['predictor_range']):>22}  "
                    f"n={b['n']:<5} mean={b['mean_return_pct']:+7.3f}%  "
                    f"hit={b['hit_rate']:.0%}")
            if a["top_minus_bottom_pct"] is not None:
                log(f"     top-bottom spread: {a['top_minus_bottom_pct']:+.3f}%")
            log()

        inc = report.get("incremental")
        if inc:
            log(f"── does {inc['overlay']} add anything on top of {inc['base']}?")
            log(f"     within the top {inc['base_top_slice']['frac']:.0%} by "
                f"{inc['base']} (n={inc['base_top_slice']['n']}):")
            log(f"       low  {inc['overlay']}: n={inc['overlay_low']['n']:<5} "
                f"mean={inc['overlay_low']['mean_return_pct']:+7.3f}%  "
                f"hit={inc['overlay_low']['hit_rate']:.0%}")
            log(f"       high {inc['overlay']}: n={inc['overlay_high']['n']:<5} "
                f"mean={inc['overlay_high']['mean_return_pct']:+7.3f}%  "
                f"hit={inc['overlay_high']['hit_rate']:.0%}")
            log(f"     incremental: {inc['incremental_pct']:+.3f}% "
                f"(t={inc['t_stat']:+.2f})")
            log(f"     -> {inc['verdict']}")
            log()

    if len(rows) < MIN_PICKS or days < 20:
        log(f"WARNING: {len(rows)} picks over {days} day(s) is too little to "
            f"conclude anything. Bucket means at this size are dominated by "
            f"noise; a spread of a few percent is not evidence. Keep "
            f"snapshotting and re-run at {MIN_PICKS}+ picks over 20+ days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
