#!/usr/bin/env python3
"""test_collect.py — the collector must never lose a round.

  python3 numerai_pipeline/test_collect.py

Six months of scoring were lost by never collecting them. The failure this
guards against is subtler and worse: collecting for a year and then wiping it
on a bad fetch. Stdlib only.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from numerai_pipeline import collect  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'ok ' if cond else '!! '} {msg}")
    if not cond:
        FAILURES.append(msg)


def rnd(n, corr=0.01, resolved=True, key="corr20V2", **kw):
    r = {"roundNumber": n, "roundResolved": resolved, "roundOpenTime": "2026-05-01", key: corr}
    r.update(kw)
    return r


def run(out: Path, models, fetch):
    orig = collect.fetch_model
    collect.fetch_model = fetch
    try:
        args = []
        for m in models:
            args += ["--model", m]
        return collect.main(args + ["--out", str(out)])
    finally:
        collect.fetch_model = orig


def rows_of(out: Path):
    p = out / "rounds.csv"
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    print("first collection")
    out = Path(tempfile.mkdtemp())
    rc = run(out, ["m1"], lambda m: ([rnd(1200 + i, 0.01 * i) for i in range(5)], "stub"))
    rows = rows_of(out)
    check(rc == 0 and len(rows) == 5, f"writes every round ({len(rows)})")
    check(all(r["corr_metric"] == "corr20V2" for r in rows),
          "records WHICH metric field was read")

    print("a failed fetch must not destroy history")
    rc = run(out, ["m1"], lambda m: ([], ""))
    after = rows_of(out)
    check(rc == 1, "exits non-zero so a scheduler can see it")
    check(len(after) == 5, f"history survives untouched ({len(after)} rows)")
    status = json.loads((out / "status.json").read_text())
    check(status["failed"] == ["m1"], "status.json names the failed model")

    print("upsert, not append")
    rc = run(out, ["m1"], lambda m: ([rnd(1200 + i, 0.01 * i) for i in range(5)], "stub"))
    check(len(rows_of(out)) == 5, "re-collecting the same rounds adds no duplicates")

    print("an unresolved round that later resolves")
    out2 = Path(tempfile.mkdtemp())
    run(out2, ["m1"], lambda m: ([rnd(1300, corr=None, resolved=False)], "stub"))
    first = rows_of(out2)[0]
    check(first["round_resolved"] == "false" and first["corr"] == "",
          "stored unresolved, with no correlation")
    run(out2, ["m1"], lambda m: ([rnd(1300, corr=0.042, resolved=True)], "stub"))
    now = rows_of(out2)
    check(len(now) == 1, "updates in place rather than adding a row")
    check(now[0]["round_resolved"] == "true" and abs(float(now[0]["corr"]) - .042) < 1e-9,
          "the resolved score lands")
    check(now[0]["first_seen_utc"] == first["first_seen_utc"],
          "first_seen_utc is never overwritten")

    print("a renamed API field shows up as data, not as zeros")
    out3 = Path(tempfile.mkdtemp())
    run(out3, ["m1"], lambda m: ([rnd(1400, 0.03, key="corrV4")], "stub"))
    r = rows_of(out3)[0]
    check(r["corr_metric"] == "corrV4" and abs(float(r["corr"]) - .03) < 1e-9,
          "reads a differently-named correlation field and records the name")
    out4 = Path(tempfile.mkdtemp())
    run(out4, ["m1"], lambda m: ([{"roundNumber": 1500, "roundResolved": True,
                                   "somethingNew": 0.05}], "stub"))
    r4 = rows_of(out4)[0]
    check(r4["corr"] == "" and r4["corr_metric"] == "",
          "an unrecognised field yields blank, never a fabricated 0.0")

    print("several models share one file")
    out5 = Path(tempfile.mkdtemp())
    run(out5, ["a", "b"], lambda m: ([rnd(1200, 0.01)], "stub"))
    check(len(rows_of(out5)) == 2, "both models stored")
    check({r["model"] for r in rows_of(out5)} == {"a", "b"}, "keyed by model")

    print("one model failing must not lose the other")
    run(out5, ["a", "b"], lambda m: (([], "") if m == "a" else ([rnd(1201, .02)], "s")))
    rows5 = rows_of(out5)
    check(len(rows5) == 3 and any(r["model"] == "a" for r in rows5),
          "model a's prior rounds are still there after its fetch failed")

    print("guards")
    check(collect.main(["--out", str(out5)]) == 2, "no model given is a config error")
    check(collect.pick({"x": 1}, ["corr"]) == (None, None), "pick returns nothing, not zero")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
