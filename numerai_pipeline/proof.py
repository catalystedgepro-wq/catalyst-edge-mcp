#!/usr/bin/env python3
"""proof.py — one command: fetch every Numerai round we were scored on, save
the raw evidence, and print the verdict.

Self-contained. No imports from this repo, no config, nothing to deploy.
Run it anywhere numer.ai is reachable.

    python3 proof.py catalystedge

It writes numerai_proof_<model>.json (the raw API response, unmodified — that
is the evidence) and prints an analysis plus a short block to paste back.

Why this exists: we have submitted the percentile rank of convergence_score to
Numerai Signals weekly since at least round 1253 (2026-04-30) and never read a
single score back. submit_numerai.py records that a submission was accepted;
nothing recorded how it did.

Stdlib only, except numerapi if it happens to be installed.
"""

from __future__ import annotations

import json
import math
import re
import statistics as st
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api-tournament.numer.ai/graphql"
# Metric field names, newest first. Verified 2026-09-19 against
# numerai/numerai-tools scoring.py, which defines alpha() and
# meta_portfolio_contribution() as first-class scoring functions, and against
# Numerai's Feb-2026 announcement that Alpha replaced corr20V2 as the Signals
# headline metric. The older names are kept as fallbacks for historical rounds
# that were scored under them. Whichever name is found is recorded per row, so
# the next rename shows up as data instead of a column of blanks.
CORR_KEYS = ["alpha", "corr20V2", "corr20d", "corrV4", "corr20", "corr", "icV2", "ic"]
MMC_KEYS = ["mpc", "mmc", "mmcV2"]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick(row, keys):
    for k in keys:
        v = num(row.get(k))
        if v is not None:
            return v, k
    return None, None


def gql(query, variables):
    req = urllib.request.Request(
        API, method="POST",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "CatalystEdge/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())


def _query_with_fallback(profile: str, model: str, fields: list[str],
                         gql_fn) -> tuple[list, list[str], str]:
    """Ask for every candidate field; drop the ones the schema rejects; retry.

    GraphQL fails the WHOLE query if any single requested field is unknown, so
    a list of candidate metric names cannot simply be concatenated — one stale
    name blanks everything. Numerai's error names the offending field, so this
    strips it and asks again until the query is accepted. That is why the
    order of CORR_KEYS does not matter and a rename cannot silently zero a
    column: whatever survives is what the schema actually has.
    """
    wanted = list(dict.fromkeys(fields))
    dropped: list[str] = []
    for _ in range(len(fields) + 2):
        q = ("query($m: String!) { %s(modelName: $m) { roundModelPerformances "
             "{ roundNumber roundResolved roundOpenTime %s } } }"
             % (profile, " ".join(wanted)))
        try:
            d = gql_fn(q, {"m": model})
        except Exception as e:  # noqa: BLE001
            return [], dropped, f"{type(e).__name__}: {e}"
        errs = d.get("errors") or []
        if errs:
            msg = errs[0].get("message", "")
            bad = re.findall(r"Cannot query field [\"']([^\"']+)[\"']", msg)
            bad += re.findall(r"field [\"']([^\"']+)[\"'] .*doesn't exist", msg)
            hit = [b for b in bad if b in wanted]
            if hit:
                for b in hit:
                    wanted.remove(b)
                    dropped.append(b)
                if not wanted:
                    # Every optional field was rejected. roundNumber,
                    # roundResolved and roundOpenTime are in the template and
                    # always requested, so the query is still valid — go once
                    # more and take the rounds without any metric rather than
                    # returning nothing.
                    continue
                continue
            return [], dropped, msg[:140]
        node = (d.get("data") or {}).get(profile) or {}
        return node.get("roundModelPerformances") or [], dropped, ""
    return [], dropped, "gave up stripping fields"


def fetch(model):
    attempts = []
    try:
        import numerapi
        rows = numerapi.SignalsAPI().daily_model_performances(model)
        if rows:
            return rows, "numerapi"
        attempts.append("numerapi returned nothing")
    except ImportError:
        attempts.append("numerapi not installed (pip install numerapi)")
    except Exception as e:  # noqa: BLE001
        attempts.append(f"numerapi: {type(e).__name__}: {e}")

    fields = " ".join(dict.fromkeys(CORR_KEYS + MMC_KEYS + ["payout"]))
    for profile in ("v3UserProfile", "v2SignalsProfile", "signalsUserProfile"):
        rows, dropped, err = _query_with_fallback(
            profile, model, CORR_KEYS + MMC_KEYS + ["tc"] + ["payout"], gql)
        if dropped:
            attempts.append(f"{profile}: schema rejected {', '.join(dropped)}")
        if rows:
            return rows, profile
        if err:
            attempts.append(f"{profile}: {err}")
    return [], "\n  ".join(attempts)


def main(argv):
    if len(argv) < 2:
        print("usage: python3 proof.py <model-name>     e.g. catalystedge")
        return 2
    model = argv[1]
    print(f"fetching every scored round for '{model}' ...\n")

    rows, source = fetch(model)
    if not rows:
        print("COULD NOT FETCH. What was tried:\n  " + source)
        print("\nIf every attempt failed on the schema, run:  pip install numerapi")
        print("and try again — it tracks Numerai's field renames.")
        return 1

    out = Path(f"numerai_proof_{model}.json")
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"source: {source}")
    print(f"raw evidence saved: {out.resolve()}  ({len(rows)} rounds)\n")

    scored = []
    for r in rows:
        c, key = pick(r, CORR_KEYS)
        if c is not None and r.get("roundResolved") in (True, None):
            scored.append((int(r.get("roundNumber", 0)), c, pick(r, MMC_KEYS)[0], key))
    if len(scored) < 5:
        print(f"only {len(scored)} resolved+scored rounds — too few to conclude.")
        return 0

    scored.sort()
    corrs = [c for _, c, _, _ in scored]
    n = len(corrs)
    mean = st.mean(corrs)
    sd = st.stdev(corrs) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd else 0.0
    pos = sum(1 for c in corrs if c > 0)
    metric = scored[0][3]

    print("=" * 66)
    print(f"  model             {model}")
    print(f"  metric            {metric}")
    print(f"  scored rounds     {n}   ({scored[0][0]} .. {scored[-1][0]})")
    print(f"  mean correlation  {mean:+.5f}")
    print(f"  median            {st.median(corrs):+.5f}")
    print(f"  std dev           {sd:.5f}")
    print(f"  t vs zero         {t:+.2f}")
    print(f"  rounds positive   {pos}/{n}  ({pos/n:.0%})")
    print(f"  best / worst      {max(corrs):+.5f} / {min(corrs):+.5f}")
    mmcs = [m for _, _, m, _ in scored if m is not None]
    if mmcs:
        print(f"  mean MMC/TC       {st.mean(mmcs):+.5f}")
    print("=" * 66)

    print("\nVERDICT")
    if abs(t) < 2:
        print(f"  Not distinguishable from zero (t={t:+.2f}). Over {n} rounds")
        print("  that is itself a result: the signal is not adding predictive")
        print("  value, and is not reliably inverted either.")
    elif mean < 0:
        print(f"  Reliably NEGATIVE (t={t:+.2f}).")
        print("  The submission ranks bullish by convergence_score, so a")
        print("  consistently negative correlation means the score is")
        print("  backwards. Inverting is one line in build_numerai_signals.py:")
        print("  load_convergence_rank() sorts ascending — reverse it.")
        print(f"  Expected correlation after inverting: about {-mean:+.5f}.")
    else:
        print(f"  Reliably POSITIVE (t={t:+.2f}). The signal has out-of-sample")
        print("  alpha on a 20-day horizon after neutralisation. This")
        print("  contradicts the internal next-day ledger, and that")
        print("  disagreement is worth understanding before changing anything.")

    print("\n--- paste this back ---")
    print(json.dumps({"model": model, "metric": metric, "rounds": n,
                      "first_round": scored[0][0], "last_round": scored[-1][0],
                      "mean_corr": round(mean, 5), "median_corr": round(st.median(corrs), 5),
                      "std": round(sd, 5), "t": round(t, 2),
                      "positive_rounds": f"{pos}/{n}",
                      "per_round": [[rd, round(c, 5)] for rd, c, _, _ in scored],
                      "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
