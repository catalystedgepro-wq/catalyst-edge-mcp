#!/usr/bin/env python3
"""numerai_scores.py — pull back what Numerai has been scoring, and read it.

The Catalyst Edge Signals submission is the percentile rank of
convergence_score (build_numerai_signals.py, plus a +/-0.10 DCF tilt). So
Numerai has been grading that exact score, weekly, out-of-sample, neutralised
against its own risk factors, by a party with no reason to flatter it — for
six months.

That is a far better referee than any internal backtest:

  internal ledger            Numerai
  32 pick dates              ~20+ resolved rounds
  next-day, self-computed    20-day forward, scored by them
  raw returns                neutralised against their factors
  our own join logic         no dependence on our code at all

  python3 -m backtest.numerai_scores --model YOUR_MODEL_NAME
  python3 -m backtest.numerai_scores --from-json rounds.json

THE PREDICTION THIS TESTS
The internal ledger says convergence_score is anti-predictive: its top decile
is 70% short/RegSHO names that underperform, and rescoring to remove that
weight improves every column. The Numerai submission ranks BULLISH by that
same score. So this should come back with mean correlation at or below zero.

If it does, that is independent confirmation on six times the data, and the
fix is a sign flip. If correlation is meaningfully positive, the internal
finding is wrong or specific to the next-day horizon, and that discrepancy is
worth more than either result alone. Write down which you expect before
running it.

NOTE ON THE API: Numerai's GraphQL schema for Signals metrics has changed
several times (corr20, corr20V2, corrV4, ...). This tries numerapi first
because it tracks those changes, then falls back to raw GraphQL, then to a
JSON file you export yourself. The schema could not be verified from the
environment this was written in — huggingface and numer.ai are both blocked
there — so if the fetch fails, use --from-json and the analysis still runs.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
import urllib.request
from pathlib import Path

API = "https://api-tournament.numer.ai/graphql"

# Metric names Numerai has used for Signals correlation, newest first.
CORR_KEYS = ["corr20V2", "corrV4", "corr20", "corr", "ic", "icV2"]
MMC_KEYS = ["mmc", "mmcV2", "tc"]


def log(msg: str = "") -> None:
    print(msg)


def _graphql(query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        API, method="POST",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "CatalystEdge/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_rounds(model: str) -> list[dict]:
    """Best effort. Returns [] and explains itself rather than guessing."""
    try:
        import numerapi  # noqa: F401
        napi = numerapi.SignalsAPI()
        rows = napi.daily_model_performances(model)
        log(f"fetched {len(rows)} rows via numerapi")
        return rows
    except ImportError:
        log("numerapi not installed (pip install numerapi) — trying raw GraphQL")
    except Exception as e:  # noqa: BLE001
        log(f"numerapi call failed: {type(e).__name__}: {e} — trying raw GraphQL")

    for profile in ("v3UserProfile", "v2SignalsProfile", "signalsUserProfile"):
        q = ("query($m: String!) { %s(modelName: $m) { roundModelPerformances "
             "{ roundNumber roundResolved %s } } }"
             % (profile, " ".join(CORR_KEYS[:3] + MMC_KEYS[:1])))
        try:
            d = _graphql(q, {"m": model})
        except Exception as e:  # noqa: BLE001
            log(f"  {profile}: {type(e).__name__}")
            continue
        if d.get("errors"):
            log(f"  {profile}: {d['errors'][0].get('message', '')[:90]}")
            continue
        node = (d.get("data") or {}).get(profile) or {}
        rows = node.get("roundModelPerformances") or []
        if rows:
            log(f"fetched {len(rows)} rounds via {profile}")
            return rows
    return []


def pick(row: dict, keys: list[str]):
    for k in keys:
        v = row.get(k)
        if v is not None:
            return float(v), k
    return None, None


def analyse(rows: list[dict]) -> int:
    resolved = [r for r in rows
                if r.get("roundResolved") in (True, None)
                and pick(r, CORR_KEYS)[0] is not None]
    if len(resolved) < 5:
        log(f"only {len(resolved)} resolved rounds with a correlation — "
            f"too few to say anything.")
        return 1

    corrs, key = [], None
    for r in resolved:
        v, k = pick(r, CORR_KEYS)
        corrs.append(v)
        key = key or k
    n = len(corrs)
    mean = st.mean(corrs)
    sd = st.stdev(corrs) if n > 1 else 0.0
    sem = sd / math.sqrt(n) if n else 0.0
    t = mean / sem if sem else 0.0
    pos = sum(1 for c in corrs if c > 0)

    log(f"\n{'='*70}\nNUMERAI SCORED PERFORMANCE\n{'='*70}")
    log(f"  metric            {key}")
    log(f"  resolved rounds   {n}")
    log(f"  mean correlation  {mean:+.5f}")
    log(f"  median            {st.median(corrs):+.5f}")
    log(f"  std dev           {sd:.5f}")
    log(f"  t vs zero         {t:+.2f}")
    log(f"  rounds positive   {pos}/{n}  ({pos/n:.0%})")
    log(f"  best / worst      {max(corrs):+.5f} / {min(corrs):+.5f}")

    mmcs = [pick(r, MMC_KEYS)[0] for r in resolved]
    mmcs = [m for m in mmcs if m is not None]
    if mmcs:
        log(f"  mean MMC/TC       {st.mean(mmcs):+.5f}  "
            f"({sum(1 for m in mmcs if m > 0)}/{len(mmcs)} positive)")

    log(f"\n{'='*70}\nVERDICT\n{'='*70}")
    if abs(t) < 2:
        log(f"  Correlation is not distinguishable from zero (t={t:+.2f}).")
        log("  Over this many rounds that is itself informative: the signal is")
        log("  not adding predictive value, and it is not reliably inverted")
        log("  either. Consistent with the internal ledger, which found no")
        log("  separation from convergence_score at any horizon.")
    elif mean < 0:
        log(f"  Reliably NEGATIVE (t={t:+.2f}).")
        log("  This is the internal finding confirmed on independent, "
            "out-of-sample,")
        log("  factor-neutralised data. A signal that is consistently wrong is")
        log("  worth exactly as much as one that is consistently right: invert")
        log("  it. In build_numerai_signals.py that is one line — rank")
        log("  descending instead of ascending.")
        log(f"  Expected correlation after inverting: about {-mean:+.5f}.")
    else:
        log(f"  Reliably POSITIVE (t={t:+.2f}).")
        log("  This CONTRADICTS the internal ledger, which found convergence_"
            "score")
        log("  anti-predictive on 32 next-day outcomes. Numerai scores a 20-day")
        log("  forward return, neutralised against its factors, on a different")
        log("  universe. Both can be true: a score that is bad for next-day")
        log("  mean-reversion can be good over a month. Do not act on either")
        log("  until you know which horizon you are trading.")
    log("\n  Numerai scores 20-day forward returns after neutralising against")
    log("  their risk factors; the internal ledger measures raw next-day moves.")
    log("  They are different questions, and a disagreement is a finding.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="Numerai Signals model name")
    ap.add_argument("--from-json", type=Path,
                    help="round performances exported as JSON (a list of objects)")
    ap.add_argument("--save", type=Path, help="write fetched rounds here")
    args = ap.parse_args(argv)

    if args.from_json:
        rows = json.loads(args.from_json.read_text())
        if isinstance(rows, dict):
            for k in ("roundModelPerformances", "data", "rounds"):
                if isinstance(rows.get(k), list):
                    rows = rows[k]
                    break
        log(f"loaded {len(rows)} rows from {args.from_json}")
    elif args.model:
        rows = fetch_rounds(args.model)
        if not rows:
            log("\nCould not fetch. Two ways forward:")
            log("  pip install numerapi   and re-run, or")
            log("  open https://signals.numer.ai/<your-model>, export the round")
            log("  history, and pass it with --from-json.")
            log("The analysis needs only: roundNumber, a correlation field, and")
            log("optionally mmc/tc.")
            return 2
        if args.save:
            args.save.write_text(json.dumps(rows, indent=2, default=str))
            log(f"saved to {args.save}")
    else:
        ap.error("pass --model or --from-json")
    return analyse(rows)


if __name__ == "__main__":
    raise SystemExit(main())
