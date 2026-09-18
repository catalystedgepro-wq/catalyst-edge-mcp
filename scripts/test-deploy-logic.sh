#!/usr/bin/env bash
# Exercise deploy.sh's droplet-side logic against a sandbox, with no droplet.
#
#   ./scripts/test-deploy-logic.sh
#
# The block deploy.sh pipes to `ssh ... bash -s` is the part that can lose
# customer API keys or leave a broken release running, and it is the part you
# cannot safely try out on production. This extracts that exact block from
# deploy.sh (never a copy — a drift would defeat the point) and runs it
# against a fake /opt/catalyst with stubbed systemctl and curl.
#
# Covers:
#   1. a good release lands, and mcp_keys.json / .env survive it
#   2. a failed health check rolls back, and the secrets survive that too
#   3. a release that does not compile never reaches a restart
#   4. old snapshots are pruned to the newest 10
#
# Exit 0 = all four pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SB="$(mktemp -d -t catalyst-deploy-test-XXXXXX)"
TARBALL=/tmp/catalyst-release.tgz   # the path the droplet-side block expects
trap 'rm -rf "$SB" "$TARBALL"' EXIT

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no()   { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
check() { if eval "$1"; then ok "$2"; else no "$2"; fi; }

APP="$SB/opt/catalyst/mcp_server"
mkdir -p "$APP" "$SB/opt/catalyst/docs/data" "$SB/bin"

# A fake droplet mid-life: an old release plus the two files a deploy must
# never touch.
cd "$REPO_ROOT"
cp catalyst_mcp.py smoke_test.py server.json "$APP/"
sed -i 's/^SERVER_VERSION = .*/SERVER_VERSION = "0.0.1-OLD"/' "$APP/catalyst_mcp.py"
printf '{"keys":{"live-customer-key":"intelligence"}}\n' > "$APP/mcp_keys.json"
printf 'TRADIER_TOKEN=secret-token\n' > "$SB/opt/catalyst/.env"

# Stubs. systemctl records what was on disk at each restart, so we can tell a
# deploy restart from the rollback's restart. curl fails when the flag is set.
cat > "$SB/bin/systemctl" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = "restart" ]; then
  v="$(sed -n 's/^SERVER_VERSION = "\(.*\)"/\1/p' "$REMOTE_APP/catalyst_mcp.py" 2>/dev/null)"
  python3 -m py_compile "$REMOTE_APP/catalyst_mcp.py" 2>/dev/null \
    && echo "restart on-disk=${v:-?} compiles"  >> "$SB_ROOT/restarts.log" \
    || echo "restart on-disk=${v:-?} BROKEN"    >> "$SB_ROOT/restarts.log"
fi
exit 0
EOS
cat > "$SB/bin/curl" <<'EOS'
#!/usr/bin/env bash
[ -f "$SB_ROOT/HEALTH_FAILS" ] && exit 22
echo '{"status":"ok","server":"catalyst-edge","version":"1.0.2"}'
EOS
chmod +x "$SB/bin/systemctl" "$SB/bin/curl"

# Extract the droplet-side block verbatim from deploy.sh.
python3 - "$SB/remote.sh" <<'PY'
import sys, pathlib
lines = pathlib.Path("scripts/deploy.sh").read_text().splitlines()
start = next(i for i, l in enumerate(lines) if l.endswith("bash -s <<'REMOTE'"))
end   = next(i for i, l in enumerate(lines) if l.strip() == "REMOTE" and i > start)
pathlib.Path(sys.argv[1]).write_text("\n".join(lines[start+1:end]) + "\n")
PY

git archive --format=tar HEAD | gzip > "$SB/release.tgz"

run_remote() {
  env -i PATH="$SB/bin:/usr/bin:/bin" SB_ROOT="$SB" REV="${1:-testrev}" \
    REMOTE_ROOT="$SB/opt/catalyst" REMOTE_APP="$APP" SERVICE=catalyst-mcp \
    HEALTH_URL=http://127.0.0.1:8848/health \
    bash "$SB/remote.sh" >"$SB/out.log" 2>&1
}

echo "1. a good release lands, secrets survive"
cp "$SB/release.tgz" "$TARBALL"
run_remote good && rc=0 || rc=$?
check "[ $rc -eq 0 ]"                                          "deploy succeeded"
check "grep -q 'SERVER_VERSION = \"1.0.2\"' '$APP/catalyst_mcp.py'"  "new code landed"
check "grep -q live-customer-key '$APP/mcp_keys.json'"         "mcp_keys.json preserved"
check "grep -q secret-token '$SB/opt/catalyst/.env'"           ".env preserved"
check "[ -x '$APP/scripts/runner.sh' ]"                        "exec bits survived git archive"
check "[ ! -f '$TARBALL' ]"                                    "staged tarball cleaned up"

echo "2. failed health check rolls back"
sed -i 's/^SERVER_VERSION = .*/SERVER_VERSION = "0.0.1-OLD"/' "$APP/catalyst_mcp.py"
touch "$SB/HEALTH_FAILS"; cp "$SB/release.tgz" "$TARBALL"
run_remote health-fail && rc=0 || rc=$?
rm -f "$SB/HEALTH_FAILS"
check "[ $rc -eq 1 ]"                                          "deploy reported failure"
check "grep -q '0.0.1-OLD' '$APP/catalyst_mcp.py'"             "rolled back to previous release"
check "grep -q live-customer-key '$APP/mcp_keys.json'"         "mcp_keys.json survived rollback"

echo "3. a release that does not compile never reaches a restart"
rm -rf "$SB/stage"; mkdir -p "$SB/stage"; tar xzf "$SB/release.tgz" -C "$SB/stage"
printf '\ndef broken(:\n    pass\n' >> "$SB/stage/catalyst_mcp.py"
tar czf "$TARBALL" -C "$SB/stage" .
: > "$SB/restarts.log"
run_remote broken && rc=0 || rc=$?
check "[ $rc -eq 1 ]"                                          "deploy reported failure"
check "! grep -q BROKEN '$SB/restarts.log'"                    "no restart with broken code on disk"
check "grep -q '0.0.1-OLD' '$APP/catalyst_mcp.py'"             "broken release rolled back"

echo "4. snapshots pruned to the newest 10"
for i in $(seq -w 1 15); do mkdir -p "$SB/opt/catalyst/releases/202601${i}T000000Z"; done
cp "$SB/release.tgz" "$TARBALL"
run_remote prune && rc=0 || rc=$?
kept="$(find "$SB/opt/catalyst/releases" -mindepth 1 -maxdepth 1 -type d | wc -l)"
check "[ $rc -eq 0 ]"                                          "deploy succeeded"
check "[ '$kept' -eq 10 ]"                                     "pruned to 10 (kept $kept)"

echo
if [ "$fail" -eq 0 ]; then
  echo "all ${pass} checks passed"
else
  echo "${fail} of $((pass+fail)) checks FAILED"
  echo "--- last run output ---"; cat "$SB/out.log"
fi
exit $(( fail > 0 ))
