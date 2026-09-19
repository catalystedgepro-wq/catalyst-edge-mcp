#!/usr/bin/env python3
"""cuts.py — subgroup analysis over the outcome ledger.

Answers a different question from evaluate.py. That one ranks a continuous
predictor; this one asks whether any *subgroup* — a conviction level, a fired
signal, a combination — has outcomes that differ from the rest.

  python3 -m backtest.cuts --data-repo ../sec-catalyst-data --conviction
  python3 -m backtest.cuts --data-repo ../sec-catalyst-data --signals
  python3 -m backtest.cuts --data-repo ../sec-catalyst-data --hold 5

SUBGROUP ANALYSIS IS WHERE BACKTESTS GO TO DIE
There are 68 distinct signal tokens and ~900 *_pts columns in this data. Test
them all at p < 0.05 and roughly 45 come back "significant" on pure noise.
Every test here is therefore reported against a Bonferroni-corrected
threshold for the number of tests actually run, and the uncorrected count is
printed beside it so the gap is visible.

Stdlib only.
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest.evaluate import (TARGETS, _num, find_ledger, load_ledger,  # noqa: E402
                               welch_t)

DEFAULT_TARGET = "alpha_close_pct"
MIN_GROUP = 30


def log(msg: str = "") -> None:
    print(msg)


def join_boards(rows: list[dict], repo: Path, columns: list[str],
                pattern: str = "snapshots/*/convergence_alerts.csv") -> int:
    """Attach board columns to ledger rows on (list_date, ticker)."""
    index: dict[tuple[str, str], dict] = {}
    for fp in sorted(glob.glob(str(repo / pattern))):
        day = Path(fp).parent.name
        try:
            with open(fp, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    t = (r.get("ticker") or "").strip().upper()
                    if t:
                        index[(day, t)] = r
        except OSError:
            continue
    hits = 0
    for r in rows:
        src = index.get((r.get("list_date", ""), (r.get("ticker") or "").upper()))
        if src:
            hits += 1
            for c in columns:
                if c in src:
                    r[c] = src[c]
    return hits


def group_stats(vals: list[float]) -> dict:
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
    return {"n": n, "mean": mean, "var": var,
            "hit": sum(1 for v in vals if v > 0) / n}


def compare(inside: list[float], outside: list[float]) -> dict | None:
    """Subgroup vs everything else, with a t on the difference."""
    if len(inside) < MIN_GROUP or len(outside) < MIN_GROUP:
        return None
    a, b = group_stats(outside), group_stats(inside)
    t = welch_t(outside, inside)
    return {"n_in": b["n"], "n_out": a["n"],
            "mean_in": round(b["mean"], 4), "mean_out": round(a["mean"], 4),
            "hit_in": round(b["hit"], 4), "hit_out": round(a["hit"], 4),
            "delta": round(b["mean"] - a["mean"], 4),
            "t": round(t, 3) if t is not None else None}


def compare_by_date(rows: list[dict], target: str, in_group) -> dict | None:
    """Subgroup vs rest, compared only WITHIN the same pick date, then pooled.

    Label meanings drift. In this ledger the conviction scheme changed
    mid-period — August boards are ~90% AVOID, September ~80% WATCH — so an
    unstratified "ELEVATED vs everything else" is mostly September vs August,
    and picks up whatever the market did in between.

    Blocking on list_date removes that entirely: within one day every pick
    faced the same tape, so a difference there is about the label.
    """
    by_day: dict[str, tuple[list, list]] = {}
    for r in rows:
        v = _num(r.get(target))
        if v is None:
            continue
        ins, outs = by_day.setdefault(r.get("list_date", ""), ([], []))
        (ins if in_group(r) else outs).append(v)

    num = den = var_sum = 0.0
    n_in = n_out = days = 0
    for ins, outs in by_day.values():
        if len(ins) < 5 or len(outs) < 5:
            continue          # a day that cannot contrast contributes nothing
        a, b = group_stats(outs), group_stats(ins)
        w = len(ins)
        num += (b["mean"] - a["mean"]) * w
        var_sum += (b["var"] / b["n"] + a["var"] / a["n"]) * w * w
        den += w
        n_in += len(ins)
        n_out += len(outs)
        days += 1
    if not den or n_in < MIN_GROUP:
        return None
    delta = num / den
    se = math.sqrt(var_sum) / den
    return {"n_in": n_in, "n_out": n_out, "days": days,
            "mean_in": None, "mean_out": None,
            "delta": round(delta, 4),
            "t": round(delta / se, 3) if se > 0 else None}


def _rank_block(ins: list[float], outs: list[float]) -> tuple[float, float, float]:
    """Mann-Whitney U for one block: (U, E[U], Var[U]) with tie correction."""
    n1, n2 = len(ins), len(outs)
    allv = [(v, 0) for v in ins] + [(v, 1) for v in outs]
    allv.sort(key=lambda p: p[0])
    ranks = [0.0] * len(allv)
    i = 0
    ties = 0.0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        t = j - i + 1
        if t > 1:
            ties += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, allv) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    eu = n1 * n2 / 2.0
    var = n1 * n2 / 12.0 * ((n + 1) - ties / (n * (n - 1))) if n > 1 else 0.0
    return u, eu, var


def compare_rank_by_date(rows: list[dict], target: str, in_group) -> dict | None:
    """Stratified rank test (van Elteren): Mann-Whitney within each pick date,
    pooled across dates.

    alpha_close_pct is violently right-skewed — in this ledger the high
    signal-count group has mean +4.3% and median -0.3%, because a handful of
    enormous winners drag every mean upward. A t-test on means is therefore
    measuring the tail, not the typical pick, and at these sample sizes its
    normal approximation does not hold. Ranks are immune to that: they ask
    whether a subgroup's picks tend to place higher than the rest on the same
    day, which is the question that was meant.
    """
    U = EU = VAR = 0.0
    n_in = n_out = days = 0
    med_in, med_out = [], []
    for day, (ins, outs) in _split_by_day(rows, target, in_group).items():
        if len(ins) < 5 or len(outs) < 5:
            continue
        u, eu, var = _rank_block(ins, outs)
        U += u
        EU += eu
        VAR += var
        n_in += len(ins)
        n_out += len(outs)
        days += 1
        med_in += ins
        med_out += outs
    if not days or n_in < MIN_GROUP or VAR <= 0:
        return None
    z = (U - EU) / math.sqrt(VAR)
    med_in.sort()
    med_out.sort()
    mi = med_in[len(med_in) // 2]
    mo = med_out[len(med_out) // 2]
    return {"n_in": n_in, "n_out": n_out, "days": days,
            "delta": round(mi - mo, 4), "median_in": round(mi, 4),
            "median_out": round(mo, 4), "t": round(z, 3)}


def _split_by_day(rows, target, in_group):
    by_day: dict[str, tuple[list, list]] = {}
    for r in rows:
        v = _num(r.get(target))
        if v is None:
            continue
        ins, outs = by_day.setdefault(r.get("list_date", ""), ([], []))
        (ins if in_group(r) else outs).append(v)
    return by_day


def bonferroni_t(n_tests: int) -> float:
    """|t| threshold for family-wise 5% across n_tests, normal approximation.

    Not exact for small samples, but the point is the order of magnitude: at
    68 tests the bar moves from 1.96 to about 3.4, and most "findings" in
    subgroup analysis live in that gap.
    """
    if n_tests <= 1:
        return 1.96
    # Inverse normal at 1 - 0.025/n via Acklam-style rational approximation.
    p = 1.0 - 0.025 / n_tests
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= 1 - pl:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def report_family(name: str, results: list[tuple[str, dict]], target: str) -> None:
    """Print a family of subgroup tests against a corrected threshold."""
    tested = [(k, v) for k, v in results if v and v["t"] is not None]
    if not tested:
        log(f"\n{name}: no subgroup large enough to test (need n >= {MIN_GROUP} "
            f"on both sides)")
        return
    thresh = bonferroni_t(len(tested))
    log(f"\n{'='*78}\n{name}  (target: {target})\n{'='*78}")
    log(f"{len(tested)} tests · uncorrected bar |t|>1.96 · "
        f"Bonferroni bar |t|>{thresh:.2f}")
    log("Stratified rank test (Mann-Whitney within each pick date, pooled): the")
    log("target is heavily right-skewed, so medians and ranks are reported, not")
    log("means. Blocking on date keeps calendar effects and label drift out.\n")
    log(f"  {'subgroup':<34}{'n':>7}{'days':>6}{'med in':>9}{'med out':>9}{'z':>8}")
    for k, v in sorted(tested, key=lambda kv: -abs(kv[1]["t"])):
        mark = "  **" if abs(v["t"]) > thresh else ("  *" if abs(v["t"]) > 1.96 else "")
        log(f"  {k:<34}{v['n_in']:>7}{v.get('days', 0):>6}"
            f"{v.get('median_in', 0):>9.3f}{v.get('median_out', 0):>9.3f}"
            f"{v['t']:>8.2f}{mark}")
    naive = sum(1 for _, v in tested if abs(v["t"]) > 1.96)
    strong = sum(1 for _, v in tested if abs(v["t"]) > thresh)
    log(f"\n  {naive} pass the uncorrected bar (~{len(tested)*0.05:.1f} expected "
        f"by chance) · {strong} survive Bonferroni")
    if strong == 0:
        log("  => nothing here separates outcomes once the search is accounted for.")


# ── conviction ───────────────────────────────────────────────────────────────

def analyse_conviction(rows: list[dict], target: str) -> None:
    levels = collections.Counter(r.get("conviction_level", "") for r in rows
                                 if r.get("conviction_level"))
    log(f"\nconviction_level distribution across joined picks: "
        f"{dict(levels.most_common())}")
    results = []
    for lvl in levels:
        results.append((lvl, compare_rank_by_date(
            rows, target, lambda r, L=lvl: r.get("conviction_level") == L)))
    skipped = [lvl for lvl, v in results if v is None]
    if skipped:
        log(f"too thin to test (need n >= {MIN_GROUP}): "
            f"{', '.join(f'{l}={levels[l]}' for l in skipped)}")
    report_family("CONVICTION LEVEL", results, target)


# ── fired signals ────────────────────────────────────────────────────────────

def split_signals(v: str) -> list[str]:
    return [s.strip() for s in (v or "").replace(";", ",").split(",") if s.strip()]


def analyse_signals(rows: list[dict], target: str, min_fires: int) -> None:
    fires = collections.Counter()
    for r in rows:
        for s in split_signals(r.get("signals_fired", "")):
            fires[s] += 1
    total = sum(1 for r in rows if r.get("signals_fired"))

    always_on = [s for s, c in fires.items() if c >= total * 0.99 and total]
    if always_on:
        log(f"\nALWAYS-ON signals ({len(always_on)}) — fire on >=99% of picks, so "
            f"they cannot discriminate\nand only inflate the score:")
        for s in sorted(always_on):
            log(f"  {s:<40} {fires[s]}/{total}")

    results = []
    for s, c in fires.items():
        if c < min_fires or s in always_on:
            continue
        results.append((s, compare_rank_by_date(
            rows, target, lambda r, S=s: S in split_signals(r.get("signals_fired", "")))))
    report_family(f"FIRED SIGNALS (>= {min_fires} fires, always-on excluded)",
                  results, target)


def analyse_pairs(rows: list[dict], target: str, min_fires: int, top: int) -> None:
    """Combinations of the most common non-constant signals.

    The search space is the point of danger: pairs from N signals is N*(N-1)/2
    tests, so this caps at the `top` most frequent and lets Bonferroni scale.
    """
    fires = collections.Counter()
    total = sum(1 for r in rows if r.get("signals_fired"))
    for r in rows:
        for s in split_signals(r.get("signals_fired", "")):
            fires[s] += 1
    cands = [s for s, c in fires.most_common()
             if c >= min_fires and not (total and c >= total * 0.99)][:top]
    results = []
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            r = compare_rank_by_date(rows, target, lambda row, A=a, B=b: (
                {A, B} <= set(split_signals(row.get("signals_fired", "")))))
            if r:
                results.append((f"{a} + {b}", r))
    report_family(f"SIGNAL PAIRS (top {len(cands)} signals)", results, target)


# ── longer holds ─────────────────────────────────────────────────────────────

def bdays(a: str, b: str) -> int:
    """Business days between two ISO dates. Exchange holidays are ignored —
    close enough to bucket a hold, not close enough to price one."""
    import datetime as dt
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    if d1 <= d0:
        return 0
    n, d = 0, d0
    while d < d1:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def analyse_hold(rows: list[dict], horizon: int, slack: int,
                 signals: list[str] | None = None) -> None:
    """Try to reconstruct an N-day hold from the ledger's own price columns.

    The ledger carries one forward day per row. A longer hold is only
    recoverable where the SAME ticker reappears on a later pick date, giving
    another dated close. That subset is self-selected — a ticker reappears
    because it keeps firing signals — so whatever it shows does not
    generalise to picks that appear once.
    """
    series: dict[str, dict[str, float]] = collections.defaultdict(dict)
    for r in rows:
        c = _num(r.get("filing_day_close"))
        if c and r.get("list_date"):
            series[r["ticker"]][r["list_date"]] = c

    built, no_exit, holds = 0, 0, []
    for r in rows:
        entry = _num(r.get("next_open"))
        t, d0 = r.get("ticker"), r.get("list_date")
        if not entry or entry <= 0 or not t or not d0:
            continue
        later = sorted(d for d in series[t] if d > d0)
        exit_px = exit_d = None
        for d in later:
            n = bdays(d0, d)
            if horizon <= n <= horizon + slack:
                exit_px, exit_d = series[t][d], d
                break
        if exit_px is None:
            no_exit += 1
            continue
        built += 1
        holds.append({"signals_fired": r.get("signals_fired", ""),
                      "ticker": t, "list_date": d0, "exit_date": exit_d,
                      "bdays": bdays(d0, exit_d),
                      "ret": (exit_px - entry) / entry * 100.0,
                      "base_score": _num(r.get("base_score")),
                      "list_name": r.get("list_name")})

    total = built + no_exit
    log(f"\n{'='*78}\nLONGER HOLD: {horizon} business days (+{slack} slack)\n{'='*78}")
    log(f"  reconstructible for {built} of {total} picks ({built/total:.1%})"
        if total else "  no picks usable")
    if not holds:
        log("  Not enough repeat appearances to reconstruct this horizon.")
        return
    uniq = len({h["ticker"] for h in holds})
    log(f"  covering {uniq} tickers · realised gap "
        f"{min(h['bdays'] for h in holds)}-{max(h['bdays'] for h in holds)} bdays")
    log("\n  SELECTION BIAS: a pick is only here because its ticker fired again")
    log("  later. Frequently-firing tickers are not a random sample of picks,")
    log("  so treat this as a hypothesis generator, never as a backtest.\n")

    rets = sorted(h["ret"] for h in holds)
    st = group_stats(rets)
    log(f"  mean {st['mean']:+.3f}%  median {rets[len(rets)//2]:+.3f}%  "
        f"hit {st['hit']:.0%}  n={st['n']}")
    log("  (mean and median diverge sharply on this target — trust the median)")
    scored = [(h["base_score"], h["ret"]) for h in holds if h["base_score"] is not None]
    if len(scored) >= 100:
        from backtest.evaluate import buckets, spearman
        rho = spearman([s for s, _ in scored], [r for _, r in scored])
        log(f"\n  base_score vs {horizon}d return: rho={rho:+.4f}")
        for b in buckets(scored, 5):
            log(f"    Q{b['bucket']}  {str(b['predictor_range']):>14}  n={b['n']:<5} "
                f"mean={b['mean_return_pct']:+8.3f}%  hit={b['hit_rate']:.0%}")
        # Rank test on the extreme quintiles, for the same reason the
        # subgroup tests use one: these returns are not remotely normal.
        srt = sorted(scored, key=lambda p: p[0])
        q = len(srt) // 5
        lo = [r for _, r in srt[:q]]
        hi = [r for _, r in srt[-q:]]
        u, eu, var = _rank_block(hi, lo)
        z = (u - eu) / math.sqrt(var) if var > 0 else 0.0
        t = welch_t(lo, hi)
        log(f"    Q5-Q1  mean-t={t:+.2f}  rank-z={z:+.2f}"
            + ("" if abs(z) > 2 else "   (rank test: noise)"))

    if signals:
        rows2 = [{"list_date": h["list_date"], "alpha_close_pct": h["ret"],
                  "signals_fired": h["signals_fired"]} for h in holds]
        r = compare_rank_by_date(rows2, "alpha_close_pct", lambda row: any(
            sg in split_signals(row.get("signals_fired", "")) for sg in signals))
        log(f"\n  signal family {signals} over {horizon}d:")
        if r:
            log(f"    n={r['n_in']} across {r['days']} days · "
                f"median in {r['median_in']:+.3f}% vs out {r['median_out']:+.3f}% · "
                f"z={r['t']:+.2f}" + ("" if abs(r["t"]) > 2 else "   (noise)"))
        else:
            log("    too few picks with a reconstructible hold to test")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-repo", required=True)
    ap.add_argument("--target", default=DEFAULT_TARGET, choices=list(TARGETS))
    ap.add_argument("--conviction", action="store_true")
    ap.add_argument("--signals", action="store_true")
    ap.add_argument("--pairs", action="store_true")
    ap.add_argument("--hold", type=int, help="reconstruct an N-business-day hold")
    ap.add_argument("--slack", type=int, default=2)
    ap.add_argument("--hold-signals", nargs="*", metavar="SIGNAL",
                    help="also test whether picks firing ANY of these signals "
                         "behave differently over the reconstructed hold")
    ap.add_argument("--min-fires", type=int, default=100)
    ap.add_argument("--top-signals", type=int, default=15)
    args = ap.parse_args(argv)

    repo = Path(args.data_repo).expanduser().resolve()
    try:
        path = find_ledger(repo)
    except FileNotFoundError as e:
        log(str(e))
        return 2
    rows = load_ledger(path)
    log(f"ledger: {path.relative_to(repo)} · {len(rows)} picks")

    if args.conviction or args.signals or args.pairs:
        hits = join_boards(rows, repo, ["conviction_level", "signals_fired",
                                        "convergence_score"])
        log(f"joined board columns: {hits} of {len(rows)} picks matched")
        joined = [r for r in rows if r.get("conviction_level") or r.get("signals_fired")]
        if not joined:
            log("nothing matched — check the snapshot dates against list_date")
            return 1
    else:
        joined = rows

    if args.conviction:
        analyse_conviction(joined, args.target)
    if args.signals:
        analyse_signals(joined, args.target, args.min_fires)
    if args.pairs:
        analyse_pairs(joined, args.target, args.min_fires, args.top_signals)
    if args.hold:
        if args.hold_signals:
            join_boards(rows, repo, ["signals_fired"])
        analyse_hold(rows, args.hold, args.slack, args.hold_signals)
    if not any([args.conviction, args.signals, args.pairs, args.hold]):
        log("nothing selected — pass --conviction, --signals, --pairs or --hold N")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
