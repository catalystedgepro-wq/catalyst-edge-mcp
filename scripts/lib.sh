#!/usr/bin/env bash
# Shared configuration and helpers for the Catalyst Edge ops scripts.
#
# Every value can be overridden from the environment, e.g.
#   DROPLET=root@10.0.0.5 ./scripts/deploy.sh
#
# Sourced, not executed.

set -euo pipefail

DROPLET="${DROPLET:-root@67.205.148.181}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/catalyst}"          # DATA_ROOT for the tools
REMOTE_APP="${REMOTE_APP:-${REMOTE_ROOT}/mcp_server}" # where catalyst_mcp.py lives
SERVICE="${SERVICE:-catalyst-mcp}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8848/health}"  # droplet-local
PUBLIC_URL="${PUBLIC_URL:-https://catalystedgescanner.com/mcp/}"

# shellcheck disable=SC2206  # intentional word-splitting into an ssh arg array
SSH_OPTS=(${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=10})

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Files that live on the droplet and must survive every deploy. Both are
# gitignored, so they are never in a release tarball — the tar --exclude in
# deploy.sh is belt-and-braces. Clobbering mcp_keys.json would drop every
# customer API key; clobbering .env would drop TRADIER_TOKEN.
PROTECTED=(mcp_keys.json .env)

say()  { printf '\033[1m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33mwarn:\033[0m %s\n'  "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# One ssh invocation, consistent options. Callers pass the remote command.
remote() { ssh "${SSH_OPTS[@]}" "$DROPLET" "$@"; }

# Fail early and legibly rather than halfway through a deploy.
require_ssh() {
  remote true 2>/dev/null \
    || die "cannot reach ${DROPLET} over ssh (key loaded? host reachable?)"
}
