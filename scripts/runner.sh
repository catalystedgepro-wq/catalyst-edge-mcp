#!/usr/bin/env bash
# Unattended health + freshness + usage check for the Catalyst Edge MCP server.
#
# DESIGNED TO RUN ON THE DROPLET, not from a laptop over ssh. Everything it
# touches is local to the host, so the scheduled path needs no ssh key, no
# outbound trust, and keeps working when your laptop is closed. See
# docs/OPERATIONS.md for the systemd timer (preferred) and the cron line.
#
#   ./scripts/runner.sh            # run the checks, append a report
#   QUIET=1 ./scripts/runner.sh    # only print on failure (good for cron)
#
# WHAT IT DOES — a fixed, auditable list. Read-only, no side effects beyond
# appending to its own report file:
#   1. is the systemd unit active
#   2. does the loopback health endpoint answer, and with which version
#   3. do the data snapshots the tools serve look fresh
#   4. summarise new journal entries since the last run
#
# WHAT IT DELIBERATELY DOES NOT DO: deploy, publish, restart the service,
# call an LLM, or make any outbound request. If you want it to act on what it
# finds, add that explicitly — do not hand this script a shell.
#
# Exit 0 = everything green. Exit 1 = at least one check failed, with the
# reason on stdout, which is what systemd/cron will capture and mail.

set -euo pipefail

ROOT="${CATALYST_ROOT:-/opt/catalyst}"
SERVICE="${SERVICE:-catalyst-mcp}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8848/health}"
REPORT_DIR="${REPORT_DIR:-${ROOT}/runner-reports}"
STATE_DIR="${STATE_DIR:-${ROOT}/.runner}"
# A snapshot older than this is stale enough to be worth knowing about.
STALE_HOURS="${STALE_HOURS:-30}"

mkdir -p "$REPORT_DIR" "$STATE_DIR"
REPORT="${REPORT_DIR}/$(date -u +%Y-%m-%d).md"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

problems=()
lines=()
note() { lines+=("$*"); }
fail() { lines+=("**FAIL** $*"); problems+=("$*"); }

note "### ${NOW}"

# ── 1. unit state ────────────────────────────────────────────────────────────
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  since="$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE" 2>/dev/null || true)"
  note "- unit \`${SERVICE}\` active (since ${since:-unknown})"
else
  fail "unit \`${SERVICE}\` is not active — \`systemctl status ${SERVICE}\`"
fi

# ── 2. health endpoint ───────────────────────────────────────────────────────
if body="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)"; then
  ver="$(printf '%s' "$body" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  note "- health OK, serving version ${ver:-?}"
  # The registry listing and the running binary should agree.
  if [[ -r "${ROOT}/mcp_server/server.json" ]]; then
    declared="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
                  "${ROOT}/mcp_server/server.json" | head -1)"
    if [[ -n "$declared" && -n "$ver" && "$declared" != "$ver" ]]; then
      fail "version drift: server.json says ${declared}, running server says ${ver}"
    fi
  fi
else
  fail "health endpoint ${HEALTH_URL} did not answer"
fi

# ── 3. data freshness ────────────────────────────────────────────────────────
# Every tool reports as_of from these files' mtimes. Stale files mean the
# server is confidently serving yesterday's signals.
for f in convergence_alerts.csv orphan_sector_lean.csv sec_outcome_summary.csv \
         docs/data/theses.json; do
  path="${ROOT}/${f}"
  if [[ ! -e "$path" ]]; then
    fail "missing data snapshot: ${f}"
    continue
  fi
  age_h=$(( ( $(date +%s) - $(stat -c %Y "$path") ) / 3600 ))
  if (( age_h > STALE_HOURS )); then
    fail "stale snapshot: ${f} is ${age_h}h old (threshold ${STALE_HOURS}h)"
  else
    note "- ${f}: ${age_h}h old"
  fi
done

# ── 3b. Kronos forecasts (optional — only once the pipeline is deployed) ─────
if [[ -e "${ROOT}/kronos_forecasts.csv" ]]; then
  age_h=$(( ( $(date +%s) - $(stat -c %Y "${ROOT}/kronos_forecasts.csv") ) / 3600 ))
  if (( age_h > STALE_HOURS )); then
    fail "stale snapshot: kronos_forecasts.csv is ${age_h}h old (threshold ${STALE_HOURS}h)"
  else
    note "- kronos_forecasts.csv: ${age_h}h old"
  fi
  # A nonzero repair count means Kronos emitted bars violating high/low ordering.
  # Locate the column by header name — a positional index breaks silently
  # if kronos_pipeline/score.py FIELDS ever changes.
  repairs="$(awk -F, '
    NR==1 { for (i=1; i<=NF; i++) if ($i == "invariant_repairs") c=i; next }
    c     { s += $c }
    END   { print s+0 }' "${ROOT}/kronos_forecasts.csv" 2>/dev/null || echo 0)"
  if [[ "${repairs:-0}" != "0" ]]; then
    note "- kronos: ${repairs} OHLC invariant repair(s) in the last run"
  fi
else
  note "- kronos_forecasts.csv: not present (pipeline not deployed)"
fi

# ── 3c. backtest snapshot coverage ───────────────────────────────────────────
# A snapshot archiver that silently stopped costs evaluation days that cannot
# be recovered, so surface it rather than letting it fail quietly.
SNAP_DIR="${ROOT}/snapshots/convergence"
if [[ -d "$SNAP_DIR" ]]; then
  days="$(find "$SNAP_DIR" -name '*.csv' | wc -l | tr -d ' ')"
  latest="$(find "$SNAP_DIR" -name '*.csv' -printf '%f\n' 2>/dev/null | sort | tail -1)"
  note "- backtest snapshots: ${days} day(s), latest ${latest%.csv}"
  if [[ -n "${latest:-}" ]] && [[ "${latest%.csv}" < "$(date -u -d '3 days ago' +%F)" ]]; then
    fail "backtest snapshots stalled — newest is ${latest%.csv}"
  fi
else
  note "- backtest snapshots: not started (see docs/OPERATIONS.md § Backtesting)"
fi

# ── 4. journal since the last run ────────────────────────────────────────────
CURSOR="${STATE_DIR}/journal.cursor"
if [[ -s "$CURSOR" ]]; then
  sel=(--after-cursor "$(cat "$CURSOR")")
else
  sel=(--since "24 hours ago")
fi
raw="$(mktemp)"; trap 'rm -f "$raw"' EXIT
journalctl -u "$SERVICE" -o short-iso --no-pager --show-cursor "${sel[@]}" \
  > "$raw" 2>/dev/null || true
grep '^-- cursor:' "$raw" | tail -1 | sed 's/^-- cursor: //' > "$CURSOR" || true
entries="$(grep -vc '^--' "$raw" || true)"
note "- ${entries:-0} new journal entries"

crashes="$(grep -c 'crashed\|dispatch error\|Traceback' "$raw" || true)"
if [[ "${crashes:-0}" != "0" ]]; then
  fail "${crashes} error line(s) in the journal since the last run"
  note '```'
  while IFS= read -r l; do note "  $l"; done \
    < <(grep 'crashed\|dispatch error\|Traceback' "$raw" | tail -10)
  note '```'
fi

# Usage/lead signal. The server does not currently emit a per-request line
# (run_http suppresses the access log and logs nothing per call), so there is
# nothing here to mine yet — say so plainly instead of reporting "0 users".
if grep -q 'tools/call\|tier=' "$raw" 2>/dev/null; then
  note "- tool-call lines present; top callers:"
  while IFS= read -r l; do note "    $l"; done \
    < <(grep -o 'tier=[a-z]*' "$raw" | sort | uniq -c | sort -rn | head -5)
else
  note "- no per-request lines in the journal (server does not log tool calls;"
  note "  see docs/OPERATIONS.md § Usage logging) — usage cannot be measured yet"
fi

# ── report ───────────────────────────────────────────────────────────────────
{ printf '%s\n' "${lines[@]}"; printf '\n'; } >> "$REPORT"

if (( ${#problems[@]} > 0 )); then
  printf '%s\n' "${lines[@]}"
  printf '\n%d problem(s) — report: %s\n' "${#problems[@]}" "$REPORT"
  exit 1
fi

if [[ -z "${QUIET:-}" ]]; then
  printf '%s\n' "${lines[@]}"
  printf '\nall checks passed — report: %s\n' "$REPORT"
fi
