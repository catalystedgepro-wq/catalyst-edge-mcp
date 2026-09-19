#!/usr/bin/env python3
"""collect.py — pull Numerai's scoring of our models and keep it forever.

Run it from the same box that submits (the API must be reachable). Every run
fetches the FULL round history and upserts, so a missed day heals itself and
a round that later resolves gets updated in place.

  python3 -m numerai_pipeline.collect --model my_signals_model
  python3 -m numerai_pipeline.collect --model a --model b --out /opt/catalyst/numerai
  python3 -m numerai_pipeline.collect --model a --status-only

DESIGN RULES, EACH FROM A FAILURE ALREADY SEEN IN THIS STACK

Never lose history. The CSV is read, merged, and written through a temp file
and an atomic rename. A fetch that fails leaves the previous file untouched
and exits non-zero; it never truncates.

Never silently succeed. Every social step in the existing daily pipeline is
`python3 x.py || echo "x failed"`, so failures go green and nobody sees them.
This exits 1 on a failed fetch and 2 on misconfiguration, writes a status
JSON with the reason, and is meant to be wired WITHOUT an `|| echo`.

Never assume a schema. Numerai has renamed the Signals correlation field
several times (corr20, corr20V2, corrV4, ...). The metric name that was
actually read is stored in every row, so a rename shows up in the data
instead of silently becoming zeros.

Stdlib only, so it runs anywhere the pipeline does.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api-tournament.numer.ai/graphql"

# Newest first. Whichever is present is recorded by name in the row.
# Metric field names, newest first. Verified 2026-09-19 against
# numerai/numerai-tools scoring.py, which defines alpha() and
# meta_portfolio_contribution() as first-class scoring functions, and against
# Numerai's Feb-2026 announcement that Alpha replaced corr20V2 as the Signals
# headline metric. The older names are kept as fallbacks for historical rounds
# that were scored under them. Whichever name is found is recorded per row, so
# the next rename shows up as data instead of a column of blanks.
CORR_KEYS = ["alpha", "corr20V2", "corr20d", "corrV4", "corr20", "corr", "icV2", "ic"]
MMC_KEYS = ["mpc", "mmc", "mmcV2"]
TC_KEYS = ["tc", "tcV2"]

FIELDS = ["model", "round_number", "round_resolved", "round_open_time",
          "corr", "corr_metric", "mmc", "tc", "payout", "stake",
          "first_seen_utc", "last_updated_utc"]


def log(msg: str) -> None:
    print(f"[numerai.collect] {msg}", file=sys.stderr, flush=True)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick(row: dict, keys: list[str]) -> tuple[float | None, str | None]:
    for k in keys:
        if row.get(k) is not None:
            v = _num(row[k])
            if v is not None:
                return v, k
    return None, None


# ── fetching ─────────────────────────────────────────────────────────────────

def _graphql(query: str, variables: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        API, method="POST",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "CatalystEdge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


def fetch_model(model: str) -> tuple[list[dict], str]:
    """(rows, source). Tries numerapi first — it tracks Numerai's schema
    churn — then raw GraphQL across the profile names Numerai has used."""
    try:
        import numerapi
        rows = numerapi.SignalsAPI().daily_model_performances(model)
        if rows:
            return rows, "numerapi"
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"{model}: numerapi failed ({type(e).__name__}: {e})")

    fields = " ".join(dict.fromkeys(CORR_KEYS + MMC_KEYS + TC_KEYS
                                    + ["payout", "selectedStakeValue"]))
    for profile in ("v3UserProfile", "v2SignalsProfile", "signalsUserProfile"):
        rows, dropped, err = _query_with_fallback(
            profile, model, CORR_KEYS + MMC_KEYS + TC_KEYS + ["payout"], _graphql)
        if dropped:
            attempts.append(f"{profile}: schema rejected {', '.join(dropped)}")
        if rows:
            return rows, profile
        if err:
            attempts.append(f"{profile}: {err}")
    return [], ""


def normalise(model: str, raw: list[dict], now: str) -> list[dict]:
    out = []
    for r in raw:
        rnd = r.get("roundNumber") or r.get("roundNumber ")
        if rnd is None:
            continue
        corr, metric = pick(r, CORR_KEYS)
        mmc, _ = pick(r, MMC_KEYS)
        tc, _ = pick(r, TC_KEYS)
        out.append({
            "model": model,
            "round_number": int(rnd),
            "round_resolved": str(r.get("roundResolved", "")).lower(),
            "round_open_time": str(r.get("roundOpenTime", "") or ""),
            "corr": "" if corr is None else f"{corr:.6f}",
            "corr_metric": metric or "",
            "mmc": "" if mmc is None else f"{mmc:.6f}",
            "tc": "" if tc is None else f"{tc:.6f}",
            "payout": "" if _num(r.get("payout")) is None else f"{_num(r.get('payout')):.6f}",
            "stake": "" if _num(r.get("selectedStakeValue")) is None
                     else f"{_num(r.get('selectedStakeValue')):.6f}",
            "first_seen_utc": now,
            "last_updated_utc": now,
        })
    return out


# ── durable storage ──────────────────────────────────────────────────────────

def read_existing(path: Path) -> dict[tuple[str, int], dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {(r["model"], int(r["round_number"])): r
                for r in csv.DictReader(f) if r.get("round_number")}


def merge(existing: dict, fresh: list[dict]) -> tuple[dict, int, int]:
    """Upsert. An unresolved round that later resolves is updated in place;
    first_seen_utc is never overwritten."""
    added = updated = 0
    for row in fresh:
        key = (row["model"], row["round_number"])
        prev = existing.get(key)
        if prev is None:
            existing[key] = row
            added += 1
            continue
        changed = any(prev.get(f, "") != row.get(f, "")
                      for f in ("round_resolved", "corr", "mmc", "tc",
                                "payout", "stake"))
        if changed:
            row["first_seen_utc"] = prev.get("first_seen_utc") or row["first_seen_utc"]
            existing[key] = row
            updated += 1
    return existing, added, updated


def write_atomic(path: Path, rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for key in sorted(rows, key=lambda k: (k[0], k[1])):
            w.writerow({k: rows[key].get(k, "") for k in FIELDS})
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", default=[],
                    help="model name; repeat for several")
    ap.add_argument("--out", type=Path,
                    default=Path(os.environ.get("CATALYST_DATA_ROOT", "/opt/catalyst"))
                    / "numerai",
                    help="directory for rounds.csv and status.json")
    ap.add_argument("--status-only", action="store_true",
                    help="report what has been collected and exit")
    args = ap.parse_args(argv)

    models = args.model or [m for m in
                            os.environ.get("NUMERAI_MODELS", "").split(",") if m.strip()]
    rounds_csv = args.out / "rounds.csv"
    status_json = args.out / "status.json"

    if args.status_only:
        existing = read_existing(rounds_csv)
        by_model: dict[str, list[int]] = {}
        for (m, rnd), row in existing.items():
            if row.get("corr"):
                by_model.setdefault(m, []).append(rnd)
        print(json.dumps({"file": str(rounds_csv), "rows": len(existing),
                          "scored_rounds": {m: {"n": len(v), "first": min(v),
                                                "last": max(v)}
                                            for m, v in by_model.items()}}, indent=2))
        return 0

    if not models:
        log("no models given — pass --model or set NUMERAI_MODELS")
        return 2

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    existing = read_existing(rounds_csv)
    before = len(existing)
    total_added = total_updated = 0
    failures = []

    for model in models:
        raw, source = fetch_model(model)
        if not raw:
            log(f"{model}: FETCH FAILED — history left untouched")
            failures.append(model)
            continue
        rows = normalise(model, raw, now)
        existing, added, updated = merge(existing, rows)
        total_added += added
        total_updated += updated
        scored = sum(1 for r in rows if r["corr"])
        log(f"{model}: {len(rows)} rounds via {source} "
            f"({scored} scored) · +{added} new, {updated} updated")

    if total_added or total_updated:
        write_atomic(rounds_csv, existing)
    log(f"{rounds_csv}: {before} -> {len(existing)} rows")

    status = {"checked_utc": now, "models": models, "failed": failures,
              "rows": len(existing), "added": total_added,
              "updated": total_updated, "file": str(rounds_csv)}
    status_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = status_json.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2))
    tmp.replace(status_json)

    if failures:
        log(f"FAILED for: {', '.join(failures)} — do NOT wire this with "
            f"`|| echo`; a silent failure here costs a round permanently")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
