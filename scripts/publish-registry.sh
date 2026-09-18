#!/usr/bin/env bash
# Publish server.json to the official MCP registry, with preflight checks.
#
#   ./scripts/publish-registry.sh            # preflight, then publish
#   CHECK_ONLY=1 ./scripts/publish-registry.sh   # preflight only, never publishes
#   LOCAL=1 ./scripts/publish-registry.sh    # run the publisher here, not on the droplet
#
# Publishing runs ON THE DROPLET by default. The namespace
# com.catalystedgescanner/* is authenticated by proving control of
# catalystedgescanner.com, and the droplet is what serves that domain.
#
# The registry rejects a re-publish of an existing version, so the preflight
# refuses early rather than letting you discover it from a 4xx. Bump
# "version" in server.json (and SERVER_VERSION in catalyst_mcp.py — the
# preflight enforces that they match) before every publish.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REGISTRY="${REGISTRY:-https://registry.modelcontextprotocol.io}"

# If you authenticated the namespace some other way (github / http / a stored
# token), this is the single line to change.
PUBLISH_LOGIN_CMD="${PUBLISH_LOGIN_CMD:-mcp-publisher login dns --domain catalystedgescanner.com --private-key \"\$MCP_PUBLISHER_KEY\"}"

cd "$REPO_ROOT"

say "preflight"

# 1. server.json is well-formed, and agrees with the server binary.
eval "$(python3 - <<'PY'
import json, re, sys, pathlib
sj = json.loads(pathlib.Path("server.json").read_text())
src = pathlib.Path("catalyst_mcp.py").read_text()
m = re.search(r'^SERVER_VERSION\s*=\s*["\']([^"\']+)["\']', src, re.M)
code_ver = m.group(1) if m else ""
print(f'SJ_NAME={json.dumps(sj["name"])}')
print(f'SJ_VERSION={json.dumps(sj["version"])}')
print(f'SJ_REMOTE={json.dumps(sj["remotes"][0]["url"])}')
print(f'CODE_VERSION={json.dumps(code_ver)}')
PY
)" || die "server.json is not valid JSON, or is missing name/version/remotes"
say "  server.json: ${SJ_NAME} v${SJ_VERSION}"

[[ "$CODE_VERSION" == "$SJ_VERSION" ]] || die \
  "version drift: server.json says ${SJ_VERSION}, catalyst_mcp.py SERVER_VERSION says ${CODE_VERSION:-<unset>}"
say "  catalyst_mcp.py SERVER_VERSION matches"

# 2. That version is not already in the registry.
PUBLISHED="$(curl -fsS --max-time 20 \
  "${REGISTRY}/v0/servers?search=${SJ_NAME##*/}" \
  | python3 -c '
import json, sys
name = sys.argv[1]
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for entry in doc.get("servers", []):
    server = entry.get("server", {})
    if server.get("name") == name:
        print(server.get("version", ""))
' "$SJ_NAME" 2>/dev/null || true)"

if [[ -n "$PUBLISHED" ]]; then
  say "  already published: $(echo "$PUBLISHED" | tr '\n' ' ')"
  if grep -qx "$SJ_VERSION" <<<"$PUBLISHED"; then
    die "version ${SJ_VERSION} is already in the registry — bump server.json and catalyst_mcp.py"
  fi
else
  warn "  could not read the registry — skipping the duplicate-version check"
fi

# 3. The endpoint the listing points agents at actually answers.
if curl -fsS --max-time 15 -o /dev/null -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$SJ_REMOTE"; then
  say "  remote endpoint answers: ${SJ_REMOTE}"
else
  die "remote endpoint ${SJ_REMOTE} did not answer tools/list — fix that before listing it"
fi

# 4. The repo link in the listing should resolve to this commit's code.
if [[ -n "$(git status --porcelain)" ]]; then
  warn "  working tree is dirty — the published listing links a repo state you have not pushed"
fi
if ! git diff --quiet HEAD "@{upstream}" 2>/dev/null; then
  warn "  HEAD differs from upstream — push before publishing so the repo link matches"
fi

if [[ -n "${CHECK_ONLY:-}" ]]; then
  say "CHECK_ONLY — preflight passed, nothing published"
  exit 0
fi

say "publishing ${SJ_NAME} v${SJ_VERSION}"

if [[ -n "${LOCAL:-}" ]]; then
  command -v mcp-publisher >/dev/null || die "mcp-publisher not on PATH"
  eval "$PUBLISH_LOGIN_CMD"
  mcp-publisher publish
else
  require_ssh
  scp "${SSH_OPTS[@]}" -q server.json "${DROPLET}:${REMOTE_APP}/server.json"
  remote REMOTE_APP="$REMOTE_APP" REMOTE_ROOT="$REMOTE_ROOT" \
         PUBLISH_LOGIN_CMD="$PUBLISH_LOGIN_CMD" bash -s <<'REMOTE'
set -euo pipefail
cd "$REMOTE_APP"
command -v mcp-publisher >/dev/null \
  || { echo "mcp-publisher not on PATH on the droplet" >&2; exit 1; }
# MCP_PUBLISHER_KEY lives in the droplet's .env, never in the repo.
set -a; [ -f "${REMOTE_ROOT}/.env" ] && . "${REMOTE_ROOT}/.env"; set +a
eval "$PUBLISH_LOGIN_CMD"
mcp-publisher publish
REMOTE
fi

say "published — verify with:"
say "  curl -s '${REGISTRY}/v0/servers?search=catalyst-edge-mcp' | python3 -m json.tool | head -40"
