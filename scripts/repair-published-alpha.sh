#!/usr/bin/env bash
# repair-published-alpha.sh — one command to correct the published alpha.
#
#   ./scripts/repair-published-alpha.sh                 # dry run, changes nothing
#   ./scripts/repair-published-alpha.sh --write         # apply it
#
# The published avg_alpha_close_pct is +1.602% on sec_top_gappers. Over the
# investable universe it is -0.295%. The gap is unadjusted reverse splits:
# filing_day_close is captured before the split and next_close after it, so
# the stored pair is quoted in two different price regimes.
#
# Four steps with real ordering dependencies, which is why this exists rather
# than a list in a README:
#
#   1. fetch split-adjusted prices for the affected tickers only
#   2. repair the ledger from that adjusted series
#   3. recompute the published summary
#   4. report before/after
#
# NO STEP IS WRAPPED IN `|| true`. Every social step in the daily pipeline is,
# which is how six months of Numerai scoring went unnoticed. A half-applied
# repair is worse than none: it would leave the summary claiming a correction
# that the ledger does not support.

set -euo pipefail

ROOT="${CATALYST_DATA_ROOT:-/opt/catalyst}"
DATA_REPO="${DATA_REPO:-$ROOT/sec-catalyst-data}"
OHLCV="${OHLCV_DIR:-$ROOT/ohlcv}"
WRITE=""
[[ "${1:-}" == "--write" ]] && WRITE=1

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."

say "preflight"
command -v python3 >/dev/null || die "python3 not found"
[[ -d "$DATA_REPO" ]] || die "outcome ledger repo not found at $DATA_REPO
  clone it: git clone https://github.com/catalystedgepro-wq/sec-catalyst-data $DATA_REPO
  or set DATA_REPO=/path/to/it"
python3 - <<'PY' || die "no Tradier token — step 1 needs it (checked \$TRADIER_TOKEN and .env)"
import sys
sys.path.insert(0, ".")
from kronos_pipeline.ohlcv import _load_tradier_token
sys.exit(0 if _load_tradier_token() else 1)
PY
say "  ledger: $DATA_REPO"
say "  ohlcv:  $OHLCV"
[[ -z "$WRITE" ]] && say "  DRY RUN — pass --write to apply"

# ── 1. adjusted prices, only for the tickers that actually need them ────────
say "1/4  fetching split-adjusted prices for affected tickers"
TICKERS="$(python3 - "$DATA_REPO" <<'PY'
import csv, sys, glob, os
# Only tickers with a move large enough to be a corporate action. Fetching the
# whole board would work but takes hundreds of API calls for no extra benefit.
snaps = sorted(glob.glob(os.path.join(sys.argv[1], "snapshots/*/outcomes.csv")))
if not snaps:
    sys.exit("no snapshots/*/outcomes.csv under " + sys.argv[1])
seen = []
with open(snaps[-1], newline="") as f:
    for r in csv.DictReader(f):
        try:
            if abs(float(r.get("next_day_close_pct") or 0)) > 100:
                t = (r.get("ticker") or "").strip().upper()
                if t and t not in seen:
                    seen.append(t)
        except ValueError:
            pass
print(" ".join(seen))
PY
)"
[[ -n "$TICKERS" ]] || { say "  no rows above +/-100% — nothing to repair"; exit 0; }
say "  $(wc -w <<<"$TICKERS") ticker(s): $TICKERS"
if [[ -n "$WRITE" ]]; then
  # shellcheck disable=SC2086
  CATALYST_DATA_ROOT="$ROOT" python3 -m kronos_pipeline.ohlcv $TICKERS
else
  say "  (dry run — skipping the fetch)"
fi

# ── 2. repair the ledger ────────────────────────────────────────────────────
say "2/4  repairing the ledger from the adjusted series"
python3 -m backtest.split_repair --data-repo "$DATA_REPO" \
  ${OHLCV:+--ohlcv-dir "$OHLCV"} ${WRITE:+--write}

# ── 3. recompute the published summary ──────────────────────────────────────
say "3/4  recomputing the published summary"
LEDGER="$(ls -1 "$DATA_REPO"/snapshots/*/outcomes.csv | tail -1)"
SUMMARY="${SUMMARY_FILE:-$ROOT/outcomes_summary.csv}"
if [[ -f "$SUMMARY" ]]; then
  python3 patches/fix_published_summary.py --outcomes "$LEDGER" \
    --summary "$SUMMARY" ${WRITE:+--write}
else
  say "  $SUMMARY not found — skipping (set SUMMARY_FILE=...)"
fi

# ── 4. what changed ─────────────────────────────────────────────────────────
say "4/4  audit"
python3 -m backtest.audit_outcomes --data-repo "$DATA_REPO" | tail -8

if [[ -z "$WRITE" ]]; then
  say "dry run complete — nothing was modified. Re-run with --write."
else
  say "done. Still to do by hand, because they touch the scanner repo:"
  say "  git apply patches/0001-investable-alpha.patch   # future pipeline runs"
  say "  git apply patches/0002-site-display.patch       # what the site shows"
fi
