#!/usr/bin/env python3
"""snapshot.py — archive today's board so a backtest becomes possible later.

convergence_alerts.csv and kronos_forecasts.csv are overwritten in place on
every pipeline run. Nothing retains what was picked on a given day, so past
performance cannot be reconstructed — only accumulated going forward. This
copies each day's file into a dated archive.

  python3 -m backtest.snapshot          # archive today, if not already done
  python3 -m backtest.snapshot --force  # re-archive, overwriting today's copy

Run it AFTER the scoring pipeline, daily. Every day it does not run is a day
of evaluation data that cannot be recovered.

Snapshots are keyed by the SOURCE FILE'S mtime date, not by today's date: if
the pipeline ran at 23:50 and this runs at 00:05, the picks still belong to
the day they were generated.

Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

DATA_ROOT = Path(os.environ.get("CATALYST_DATA_ROOT", "/opt/catalyst"))
SNAP_ROOT = DATA_ROOT / "snapshots"

# (source file, archive subdirectory, required?)
SOURCES = [
    ("convergence_alerts.csv", "convergence", True),
    ("kronos_forecasts.csv", "kronos", False),
    ("orphan_sector_lean.csv", "sector_lean", False),
]


def log(msg: str) -> None:
    print(f"[backtest.snapshot] {msg}", file=sys.stderr, flush=True)


def source_date(path: Path) -> str:
    """The day the file was generated, from its mtime — same convention the
    MCP server uses for as_of, so a snapshot and a served response agree."""
    ts = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    return ts.date().isoformat()


def archive_one(name: str, subdir: str, force: bool) -> tuple[str, str]:
    """Returns (status, detail). Never overwrites unless force."""
    src = DATA_ROOT / name
    if not src.exists():
        return "missing", f"{name} not present"

    day = source_date(src)
    dest_dir = SNAP_ROOT / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{day}.csv"

    if dest.exists() and not force:
        return "exists", f"{subdir}/{day}.csv already archived"

    # Copy to .tmp then rename: a reader never sees a partial snapshot, and a
    # crash mid-copy cannot leave a truncated file that looks complete.
    tmp = dest.with_suffix(".csv.tmp")
    shutil.copy2(src, tmp)
    tmp.replace(dest)
    return "archived", f"{subdir}/{day}.csv"


def coverage() -> dict:
    """How much evaluation data has accumulated so far."""
    out = {}
    for _, subdir, _ in SOURCES:
        d = SNAP_ROOT / subdir
        days = sorted(p.stem for p in d.glob("*.csv")) if d.exists() else []
        out[subdir] = {"days": len(days),
                       "first": days[0] if days else None,
                       "last": days[-1] if days else None}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="overwrite today's snapshot if it already exists")
    ap.add_argument("--status", action="store_true",
                    help="report accumulated coverage and exit")
    args = ap.parse_args(argv)

    if args.status:
        print(json.dumps(coverage(), indent=2))
        return 0

    failed = False
    for name, subdir, required in SOURCES:
        status, detail = archive_one(name, subdir, args.force)
        if status == "missing":
            if required:
                log(f"MISSING (required): {detail}")
                failed = True
            else:
                log(f"skipped: {detail}")
        else:
            log(f"{status}: {detail}")

    cov = coverage()
    conv = cov["convergence"]
    log(f"coverage: {conv['days']} day(s) "
        f"{conv['first'] or '-'} .. {conv['last'] or '-'}")
    # A predictor comparison on a handful of days is noise, not evidence.
    if conv["days"] < 60:
        log(f"need ~60 trading days before backtest.evaluate is meaningful "
            f"({60 - conv['days']} to go)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
