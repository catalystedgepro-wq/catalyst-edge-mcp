#!/usr/bin/env bash
# finish-deployment.sh — everything still pending on the droplet, in order.
#
#   ./scripts/finish-deployment.sh            # dry run
#   ./scripts/finish-deployment.sh --write    # apply
#
# Four independent pieces, run in dependency order and reported separately so
# a failure in one does not hide the others:
#
#   A  regenerate the summary so the applied site patch has a column to read
#   B  repair the unadjusted reverse splits in the ledger
#   C  install the Numerai collector timer, and backfill immediately
#   D  report what is now true
#
# Every piece is idempotent. Re-running after a partial failure is safe.

set -uo pipefail   # deliberately NOT -e: each piece reports its own outcome

ROOT="${CATALYST_DATA_ROOT:-/opt/catalyst}"
DATA_REPO="${DATA_REPO:-$ROOT/sec-catalyst-data}"
SUMMARY="${SUMMARY_FILE:-$ROOT/outcomes_summary.csv}"
MODELS="${NUMERAI_MODELS:-catalystedge}"
WRITE=""
[[ "${1:-}" == "--write" ]] && WRITE=1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILED="$FAILED $1"; }
FAILED=""

cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -z "$WRITE" ]] && say "DRY RUN — pass --write to apply"

# ── A. summary ──────────────────────────────────────────────────────────────
say "A  regenerate the published summary"
if [[ ! -f "$SUMMARY" ]]; then
  bad "summary-missing ($SUMMARY not found; set SUMMARY_FILE)"
else
  LEDGER="$(ls -1 "$DATA_REPO"/snapshots/*/outcomes.csv 2>/dev/null | tail -1)"
  if [[ -z "$LEDGER" ]]; then
    bad "no-ledger (clone sec-catalyst-data, or set DATA_REPO)"
  elif python3 patches/fix_published_summary.py --outcomes "$LEDGER" \
        --summary "$SUMMARY" ${WRITE:+--write}; then
    ok "summary recomputed"
  else
    bad "summary"
  fi
fi

# ── B. split repair ─────────────────────────────────────────────────────────
say "B  repair unadjusted reverse splits"
if ./scripts/repair-published-alpha.sh ${WRITE:+--write} 2>&1 | tail -20; then
  ok "split repair"
else
  bad "split-repair"
fi

# ── C. Numerai collector ────────────────────────────────────────────────────
say "C  Numerai score collection"
if ! command -v systemctl >/dev/null; then
  bad "no-systemd (run 'python3 -m numerai_pipeline.collect --model $MODELS' from cron)"
elif [[ -z "$WRITE" ]]; then
  echo "  would install catalyst-numerai.{service,timer} for model: $MODELS"
  echo "  would run one backfill immediately"
else
  sed "s/REPLACE_WITH_YOUR_MODEL/$MODELS/" \
      numerai_pipeline/catalyst-numerai.service > /etc/systemd/system/catalyst-numerai.service \
    && cp numerai_pipeline/catalyst-numerai.timer /etc/systemd/system/ \
    && systemctl daemon-reload \
    && systemctl enable --now catalyst-numerai.timer \
    && ok "timer installed" || bad "timer"
  # Backfill now rather than waiting for the first firing.
  if CATALYST_DATA_ROOT="$ROOT" python3 -m numerai_pipeline.collect \
       --model "$MODELS" --out "$ROOT/numerai"; then
    ok "backfill"
  else
    bad "backfill"
  fi
fi

# ── D. state ────────────────────────────────────────────────────────────────
say "D  where things stand"
python3 -m numerai_pipeline.collect --status-only --out "$ROOT/numerai" 2>/dev/null \
  || echo "  numerai: nothing collected yet"
if [[ -f "$SUMMARY" ]]; then
  python3 - "$SUMMARY" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
has = "avg_alpha_close_pct_investable" in (rows[0] if rows else {})
print(f"  summary columns: {'investable column PRESENT' if has else 'investable column MISSING — the site still shows the raw mean'}")
for r in rows:
    if r.get("avg_alpha_close_pct_investable"):
        print(f"    {r['list_name']:24} published {float(r['avg_alpha_close_pct']):+7.4f}"
              f"  ->  investable {float(r['avg_alpha_close_pct_investable']):+7.4f}")
PY
fi

if [[ -n "$FAILED" ]]; then
  printf '\n\033[31m%s\033[0m\n' "incomplete:$FAILED"
  echo "Each piece is independent and idempotent — fix and re-run."
  exit 1
fi
printf '\n\033[32mall pieces completed\033[0m\n'
[[ -z "$WRITE" ]] && echo "(dry run — nothing was modified)"
exit 0
