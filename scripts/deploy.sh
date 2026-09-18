#!/usr/bin/env bash
# Deploy the committed tree to the droplet, restart the service, verify it.
#
#   ./scripts/deploy.sh             # deploy HEAD
#   DRY_RUN=1 ./scripts/deploy.sh   # build + show the plan, touch nothing remote
#   FORCE=1 ./scripts/deploy.sh     # allow a dirty working tree
#
# What it guarantees:
#   - only git-tracked files at HEAD are shipped; local scratch never leaves
#   - mcp_keys.json and .env on the droplet are never overwritten
#   - the release is byte-compiled before the service is restarted, so a
#     syntax error fails the deploy instead of taking the service down
#   - the previous release is snapshotted under $REMOTE_ROOT/releases and
#     restored automatically if the post-restart health check fails
#
# Rollback restores the files the old release contained. A file added by the
# new release is left in place (harmless — nothing imports it). To undo that
# too, deploy an older commit.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" && -z "${FORCE:-}" ]]; then
  die "working tree is dirty — commit first, or re-run with FORCE=1"
fi

REV="$(git rev-parse --short HEAD)"
TARBALL="$(mktemp -t catalyst-release-XXXXXX.tgz)"
trap 'rm -f "$TARBALL"' EXIT

say "building release from ${REV}"
git archive --format=tar HEAD | gzip > "$TARBALL"
say "$(du -h "$TARBALL" | cut -f1) → ${DROPLET}:${REMOTE_APP}"

if [[ -n "${DRY_RUN:-}" ]]; then
  say "DRY_RUN — files that would be deployed:"
  git ls-tree -r --name-only HEAD | sed 's/^/    /' >&2
  say "DRY_RUN — nothing sent."
  exit 0
fi

require_ssh
scp "${SSH_OPTS[@]}" -q "$TARBALL" "${DROPLET}:/tmp/catalyst-release.tgz"

# The whole remote side is one transaction: snapshot, extract, compile,
# restart, health-check, roll back on failure.
remote REV="$REV" REMOTE_ROOT="$REMOTE_ROOT" REMOTE_APP="$REMOTE_APP" \
       SERVICE="$SERVICE" HEALTH_URL="$HEALTH_URL" bash -s <<'REMOTE'
set -euo pipefail

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${REMOTE_ROOT}/releases/${TS}"

mkdir -p "${REMOTE_ROOT}/releases" "$REMOTE_APP"
cp -a "$REMOTE_APP" "$BACKUP"
echo "==> snapshot: ${BACKUP}"

rollback() {
  echo "!!! rolling back to ${BACKUP}" >&2
  cp -a "${BACKUP}/." "${REMOTE_APP}/"
  systemctl restart "$SERVICE" || true
  exit 1
}

tar xzf /tmp/catalyst-release.tgz -C "$REMOTE_APP" \
    --exclude=mcp_keys.json --exclude=.env
rm -f /tmp/catalyst-release.tgz
echo "==> extracted ${REV}"

# Catch a syntax error here, while the old process is still serving.
if ! python3 -m py_compile "${REMOTE_APP}/catalyst_mcp.py"; then
  echo "!!! ${REV} does not compile" >&2
  rollback
fi

systemctl restart "$SERVICE"
echo "==> restarted ${SERVICE}"

for _ in $(seq 1 20); do
  if body="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)"; then
    echo "==> healthy: ${body}"
    # Keep the ten most recent snapshots; drop the rest.
    ls -1d "${REMOTE_ROOT}/releases"/*/ 2>/dev/null \
      | sort -r | tail -n +11 | xargs -r rm -rf
    exit 0
  fi
  sleep 1
done

echo "!!! ${SERVICE} did not answer ${HEALTH_URL} within 20s" >&2
systemctl status "$SERVICE" --no-pager --lines=20 >&2 || true
rollback
REMOTE

say "droplet-local health check passed"

# The public path exercises nginx + TLS too, which the loopback check does not.
if curl -fsS --max-time 10 -o /dev/null \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$PUBLIC_URL"; then
  say "public endpoint OK: ${PUBLIC_URL}"
else
  warn "public endpoint ${PUBLIC_URL} did not answer — service is up, so check nginx/TLS"
fi

say "deployed ${REV}"
