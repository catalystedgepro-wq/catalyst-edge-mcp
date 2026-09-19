#!/usr/bin/env python3
"""evaluate.py — rank predictors against the real per-pick outcome ledger.

Consumes the outcome ledger published in catalystedgepro-wq/sec-catalyst-data
(`snapshots/<date>/outcomes.csv`), which records, per pick, what the ticker
actually did. Any predictor — base_score, convergence_score, a Kronos p_up —
joins into that ledger on (list_date, ticker) and is scored the same way.

  # the score that ships inside the ledger
  python3 -m backtest.evaluate --data-repo ../sec-catalyst-data

  # a predictor held in dated snapshot files alongside it
  python3 -m backtest.evaluate --data-repo ../sec-catalyst-data \
      --join-predictor convergence_score --join-glob 'snapshots/*/convergence_alerts.csv'

TWO THINGS THIS TOOL REFUSES TO DO QUIETLY
1. It does not pool lists by default. base_score spans 15..27 on
   sec_clean_gappers and -11..27 on sec_top_gappers; a pooled rank
   correlation across incompatible scales measures the mix of lists, not the
   score. Per-list is the default and pooled needs --pooled, which prints a
   warning next to the number.
2. It does not invent horizons. Every outcome column in the ledger is
   next-day, so the "horizon" axis here is the exit rule inside day one —
   open, close, VWAP, max run — not a multi-day hold. Multi-day would need
   price history the ledger does not carry.

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sys
from pathlib import Path

# Exit rules available in the ledger. All are next-day; they differ in WHERE
# in the session you get out, which is the only horizon axis this data has.
TARGETS = {
    "alpha_close_pct":      "next close vs filing close, minus SPY",
    "next_day_close_pct":   "next close vs filing close, raw",
    "gap_next_open_pct":    "next open vs filing close (the overnight gap)",
    "next_day_max_run_pct": "best intraday print (an upper bound, not tradable)",
    "next_day_vwap_pct":    "next-day VWAP vs filing close",
    "realistic_pnl_net_pct": "+2%/-1.5% bracket, net of execution cost",
}
DEFAULT_TARGETS = ["alpha_close_pct", "gap_next_open_pct", "next_day_vwap_pct",
                   "realistic_pnl_net_pct"]

MIN_N = 200   # below this a per-list read is noise


def log(msg: str = "") -> None:
    print(msg)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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



def welch_t(a: list[float], b: list[float]) -> float | None:
    """t for the difference in means. A quintile spread without this is a
    number with no error bar attached."""
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (mb - ma) / se if se > 0 else None


def assess(rows: list[dict], predictor: str, target: str) -> dict | None:
    pairs = [(_num(r.get(predictor)), _num(r.get(target))) for r in rows]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(pairs) < 20:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    bs = buckets(pairs, 5)
    t = None
    if bs:
        srt = sorted(pairs, key=lambda p: p[0])
        q = len(srt) // 5
        t = welch_t([r for _, r in srt[:q]], [r for _, r in srt[-q:]])
    return {
        "predictor": predictor, "target": target, "n": len(pairs),
        "spearman": round(spearman(xs, ys) or 0.0, 4),
        "buckets": bs,
        "q5_minus_q1_pct": (round(bs[-1]["mean_return_pct"] - bs[0]["mean_return_pct"], 4)
                            if bs else None),
        "q5_minus_q1_t": round(t, 3) if t is not None else None,
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


# ── loading the real ledger ──────────────────────────────────────────────────

def find_ledger(repo: Path) -> Path:
    """The newest snapshots/<date>/outcomes.csv. It is cumulative — the latest
    one carries every list_date — so there is no need to stitch them."""
    cands = sorted(repo.glob("snapshots/*/outcomes.csv"))
    if not cands:
        direct = repo / "data" / "outcomes.csv"
        if direct.exists():
            return direct
        raise FileNotFoundError(
            f"no snapshots/*/outcomes.csv under {repo} — point --data-repo at a "
            f"checkout of the outcome-ledger repository")
    return cands[-1]


def load_ledger(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def join_predictor(rows: list[dict], repo: Path, pattern: str,
                   column: str) -> tuple[int, int]:
    """Attach a predictor held in dated files to the ledger by (date, ticker).

    The date comes from the containing directory name, which is how the
    snapshot layout encodes it; a row only matches when that date equals the
    pick's list_date, so nothing is joined across days.
    """
    index: dict[tuple[str, str], str] = {}
    files = sorted(glob.glob(str(repo / pattern)))
    for fp in files:
        day = Path(fp).parent.name
        try:
            with open(fp, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    t = (r.get("ticker") or "").strip().upper()
                    if t and r.get(column) not in (None, ""):
                        index[(day, t)] = r[column]
        except OSError:
            continue
    hits = 0
    for r in rows:
        v = index.get((r.get("list_date", ""), (r.get("ticker") or "").upper()))
        if v is not None:
            r[column] = v
            hits += 1
    return len(files), hits


def list_containment(groups: dict[str, list[dict]]) -> list[tuple[str, list[str]]]:
    """Report which lists are wholly contained in which others.

    In the published ledger every list is a nested subset of sec_top_gappers,
    whose pick set equals the union of all six. So "six lists" is one universe
    plus five filtered views of it. Reporting them as six results multiplies
    the apparent evidence and the multiple-comparison burden for what is
    largely the same data seen repeatedly.
    """
    keys = {n: {(r.get("list_date"), r.get("ticker")) for r in g}
            for n, g in groups.items()}
    out = []
    for n in sorted(keys, key=lambda k: -len(keys[k])):
        supersets = [m for m in keys
                     if m != n and keys[n] and keys[n] <= keys[m]]
        out.append((n, sorted(supersets, key=lambda k: -len(keys[k]))))
    return out


def by_list(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.get("list_name", "?"), []).append(r)
    return out


# ── reporting ────────────────────────────────────────────────────────────────

def print_assessment(a: dict, indent: str = "  ") -> None:
    verdict = ""
    if a["q5_minus_q1_t"] is not None and abs(a["q5_minus_q1_t"]) < 2:
        verdict = "  (spread not distinguishable from noise)"
    log(f"{indent}{a['target']:<24} n={a['n']:<6} rho={a['spearman']:+.4f}  "
        f"Q5-Q1={a['q5_minus_q1_pct']:+7.3f}% "
        f"t={a['q5_minus_q1_t'] if a['q5_minus_q1_t'] is not None else float('nan'):+.2f}{verdict}")


def print_buckets(a: dict, indent: str = "      ") -> None:
    for b in a["buckets"]:
        log(f"{indent}Q{b['bucket']}  {str(b['predictor_range']):>16}  "
            f"n={b['n']:<6} mean={b['mean_return_pct']:+8.3f}%  hit={b['hit_rate']:.0%}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-repo", required=True,
                    help="checkout of the outcome-ledger repo (sec-catalyst-data)")
    ap.add_argument("--predictor", default="base_score")
    ap.add_argument("--join-predictor", help="predictor column held in dated files")
    ap.add_argument("--join-glob", default="snapshots/*/convergence_alerts.csv")
    ap.add_argument("--targets", nargs="*", default=DEFAULT_TARGETS)
    ap.add_argument("--pooled", action="store_true",
                    help="also pool every list (scales differ — read the warning)")
    ap.add_argument("--detail", action="store_true", help="print quintile tables")
    ap.add_argument("--incremental", nargs=2, metavar=("BASE", "OVERLAY"),
                    help="does OVERLAY add anything on top of BASE")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    repo = Path(args.data_repo).expanduser().resolve()
    try:
        path = find_ledger(repo)
    except FileNotFoundError as e:
        log(str(e))
        return 2
    rows = load_ledger(path)
    dates = sorted({r.get("list_date", "") for r in rows if r.get("list_date")})
    log(f"ledger: {path.relative_to(repo)}")
    log(f"  {len(rows)} picks · {len(dates)} pick dates {dates[0]}..{dates[-1]} "
        f"· {len({r['ticker'] for r in rows})} tickers")

    predictor = args.predictor
    if args.join_predictor:
        nfiles, hits = join_predictor(rows, repo, args.join_glob, args.join_predictor)
        log(f"  joined {args.join_predictor} from {nfiles} file(s): "
            f"{hits} of {len(rows)} picks matched on (list_date, ticker)")
        if hits == 0:
            log("  nothing matched — check that the snapshot directory dates line "
                "up with list_date before reading anything below")
            return 1
        rows = [r for r in rows if r.get(args.join_predictor) not in (None, "")]
        predictor = args.join_predictor

    unknown = [t for t in args.targets if t not in TARGETS]
    if unknown:
        log(f"unknown target(s): {unknown}\navailable: {list(TARGETS)}")
        return 2

    log(f"\npredictor: {predictor}")
    log("every outcome in this ledger is NEXT-DAY; the targets below are exit "
        "rules inside day one, not multi-day holds.")
    for t in args.targets:
        log(f"  {t:<24} {TARGETS[t]}")

    report = {"ledger": str(path), "picks": len(rows), "dates": len(dates),
              "predictor": predictor, "per_list": {}, "pooled": None}

    groups = by_list(rows)
    containment = list_containment(groups)
    nested = [(n, sup) for n, sup in containment if sup]
    if nested:
        log(f"\n{'='*78}\nLIST STRUCTURE\n{'='*78}")
        for n, sup in containment:
            note = f"subset of {', '.join(sup)}" if sup else "(the universe)"
            log(f"  {n:24} n={len(groups[n]):<6} {note}")
        log("\nNested lists are not independent tests. The per-list results below\n"
            "re-examine largely the same picks through different filters.")

    log(f"\n{'='*78}\nPER LIST (scales differ between lists — this is the honest cut)\n{'='*78}")
    for name, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        scores = [s for s in (_num(r.get(predictor)) for r in group) if s is not None]
        if not scores:
            continue
        thin = "   [THIN — treat as indicative only]" if len(group) < MIN_N else ""
        log(f"\n{name}  n={len(group)}  {predictor} {min(scores):.0f}..{max(scores):.0f}{thin}")
        results = []
        for t in args.targets:
            a = assess(group, predictor, t)
            if not a:
                continue
            results.append(a)
            print_assessment(a)
            if args.detail:
                print_buckets(a)
        report["per_list"][name] = results

    if args.pooled:
        log(f"\n{'='*78}\nPOOLED — WARNING\n{'='*78}")
        log("Pooling lists whose score scales differ measures the mix of lists as "
            "much as the score.\nTreat a pooled number as a sanity check, never as "
            "the result.")
        pooled = [a for a in (assess(rows, predictor, t) for t in args.targets) if a]
        for a in pooled:
            print_assessment(a)
        report["pooled"] = pooled

    if args.incremental:
        base, overlay = args.incremental
        log(f"\n{'='*78}\nINCREMENTAL: does {overlay} add anything on top of {base}?\n{'='*78}")
        for name, group in sorted(by_list(rows).items(), key=lambda kv: -len(kv[1])):
            inc = assess_incremental(group, base=base, overlay=overlay)
            if inc:
                log(f"\n{name}  n={len(group)}")
                log(f"  incremental {inc['incremental_pct']:+.3f}%  "
                    f"t={inc['t_stat']:+.2f}  -> {inc['verdict']}")

    # Every quintile spread above was one hypothesis test. Say how many.
    n_tests = sum(len(v) for v in report["per_list"].values())
    hits = sum(1 for v in report["per_list"].values() for a in v
               if a["q5_minus_q1_t"] is not None and abs(a["q5_minus_q1_t"]) >= 2)
    if n_tests:
        log(f"\n{'='*78}\nMULTIPLE COMPARISONS\n{'='*78}")
        log(f"  {n_tests} quintile-spread tests run · {hits} reached |t| >= 2")
        log(f"  expected by chance alone at 5%: ~{n_tests * 0.05:.1f}")
        if hits <= n_tests * 0.05 + 1:
            log("  => this is what pure noise looks like. Do not read the "
                "individual hits as findings.")
        else:
            log("  => more hits than chance predicts, but check whether they "
                "fall in near-duplicate lists before believing it.")
        report["multiple_comparisons"] = {"tests": n_tests, "hits": hits}

    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
