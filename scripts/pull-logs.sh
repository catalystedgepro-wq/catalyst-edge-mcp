#!/usr/bin/env bash
# Pull the MCP server's journal off the droplet into ./logs (gitignored).
#
#   ./scripts/pull-logs.sh                 # everything new since the last pull
#   SINCE='2026-09-01' ./scripts/pull-logs.sh   # absolute start, ignores cursor
#   SINCE='2 hours ago' ./scripts/pull-logs.sh
#   FOLLOW=1 ./scripts/pull-logs.sh        # stream live to the terminal
#
# Incremental pulls use a journald cursor stored in logs/.cursor, so repeated
# runs never re-download or duplicate lines. Delete that file to start over.
#
# NOTE ON LEAD-FINDING: as of this commit the server logs startup lines and
# crashes only. run_http() suppresses BaseHTTPRequestHandler's access log
# (Handler.log_message is a no-op) and no per-request line is emitted, so
# there is no record of who called which tool. This script will faithfully
# pull an almost-empty journal. See docs/OPERATIONS.md — "Usage logging".

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

LOG_DIR="${LOG_DIR:-${REPO_ROOT}/logs}"
CURSOR_FILE="${LOG_DIR}/.cursor"
mkdir -p "$LOG_DIR"

require_ssh

if [[ -n "${FOLLOW:-}" ]]; then
  say "streaming ${SERVICE} from ${DROPLET} (ctrl-c to stop)"
  exec ssh "${SSH_OPTS[@]}" -t "$DROPLET" \
    "journalctl -u ${SERVICE} -f -o short-iso --no-pager"
fi

# Cursor wins unless SINCE is given explicitly.
if [[ -n "${SINCE:-}" ]]; then
  SELECTOR=(--since "$SINCE")
  say "pulling ${SERVICE} since '${SINCE}'"
elif [[ -s "$CURSOR_FILE" ]]; then
  SELECTOR=(--after-cursor "$(cat "$CURSOR_FILE")")
  say "pulling ${SERVICE} since the last pull"
else
  SELECTOR=(--since "24 hours ago")
  say "no cursor yet — pulling the last 24h of ${SERVICE}"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${LOG_DIR}/${SERVICE}-${STAMP}.log"
RAW="$(mktemp)"
trap 'rm -f "$RAW"' EXIT

# --show-cursor appends a trailing "-- cursor: s=..." line; split it back off
# so the saved log contains log lines only.
remote journalctl -u "$SERVICE" -o short-iso --no-pager --show-cursor \
       "${SELECTOR[@]}" > "$RAW"

grep -v '^-- cursor:' "$RAW" | grep -v '^-- No entries' > "$OUT" || true
if NEW_CURSOR="$(grep '^-- cursor:' "$RAW" | tail -1 | sed 's/^-- cursor: //')" \
   && [[ -n "$NEW_CURSOR" ]]; then
  printf '%s' "$NEW_CURSOR" > "$CURSOR_FILE"
fi

LINES="$(wc -l < "$OUT" | tr -d ' ')"
if [[ "$LINES" == "0" ]]; then
  rm -f "$OUT"
  say "no new entries"
else
  say "${LINES} lines → ${OUT#"$REPO_ROOT"/}"
fi
