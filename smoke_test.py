#!/usr/bin/env python3
"""smoke_test.py — exercise the Catalyst Edge MCP server end-to-end.

Spawns catalyst_mcp.py, runs the MCP handshake, calls every tool, and checks
each returns a non-error result. Also checks free-tier gating.

Run: python3 mcp_server/smoke_test.py   (exit 0 = all good)
"""

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "catalyst_mcp.py"
ROOT = HERE.parent


# Everything the tools read. All are gitignored or live outside this repo, so
# a fresh clone has none of them — check up front and say so, rather than
# dying with a traceback partway through the run.
REQUIRED = [
    (HERE / "mcp_keys.json", "key→tier map (see mcp_keys.json.example)"),
    (ROOT / "convergence_alerts.csv", "scored picks snapshot"),
    (ROOT / "docs/data/theses.json", "per-ticker theses"),
    (ROOT / "orphan_sector_lean.csv", "sector lean snapshot"),
    (ROOT / "sec_outcome_summary.csv", "track-record outcomes"),
]


# Every tool the server registers. Kept explicit so adding a tool without
# updating the test is a failure, not a silent pass.
EXPECTED_TOOLS = {
    "get_convergence_picks", "get_ticker_signal", "get_thesis",
    "get_price_forecast", "get_sector_lean", "get_options_context",
    "get_track_record",
}

# Produced by kronos_pipeline.score. Optional: a droplet that has not deployed
# the Kronos pipeline yet is not broken, so this is tested only when present.
KRONOS_FORECASTS = ROOT / "kronos_forecasts.csv"


def missing_inputs() -> list[str]:
    return [f"{p} — {what}" for p, what in REQUIRED if not p.exists()]


def pick_ticker() -> str:
    """A ticker present in BOTH theses.json and convergence_alerts.csv —
    the two snapshots aren't always perfectly in sync."""
    theses = set(json.loads(
        (ROOT / "docs/data/theses.json").read_text()).get("theses") or {})
    with (ROOT / "convergence_alerts.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip().upper()
            if t in theses:
                return t
    return "AAPL"


def intelligence_key() -> str:
    """An intelligence-tier key from mcp_keys.json — no key is hardcoded."""
    try:
        raw = (HERE / "mcp_keys.json").read_text()
    except OSError:
        return ""
    try:
        keys = json.loads(raw).get("keys", {})
    except json.JSONDecodeError:
        return ""
    for k, tier in keys.items():
        if tier == "intelligence":
            return k
    return ""


def check_version_sync() -> list[str]:
    """server.json is what the registry publishes; SERVER_VERSION is what
    clients see in initialize and GET /health. A drift between them ships a
    listing that misdescribes the running server."""
    import re
    try:
        declared = json.loads((HERE / "server.json").read_text())["version"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        return [f"server.json unreadable: {e}"]
    m = re.search(r'^SERVER_VERSION\s*=\s*["\']([^"\']+)["\']',
                  SERVER.read_text(), re.M)
    if not m:
        return ["could not find SERVER_VERSION in catalyst_mcp.py"]
    if m.group(1) != declared:
        return [f"version drift: server.json={declared} "
                f"catalyst_mcp.py={m.group(1)}"]
    return []


def run(tier_key: str, messages: list[dict]) -> list[dict]:
    env = dict(os.environ)
    if tier_key:
        env["CATALYST_MCP_KEY"] = tier_key
    else:
        env.pop("CATALYST_MCP_KEY", None)
    inp = "\n".join(json.dumps(m) for m in messages) + "\n"
    r = subprocess.run([sys.executable, str(SERVER)], input=inp,
                       capture_output=True, text=True, timeout=30, env=env)
    return [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]


def test_http(ikey: str) -> list[str]:
    """Spawn the HTTP transport, exercise it end-to-end, return failures."""
    import time
    import urllib.error
    import urllib.request

    fails: list[str] = []
    port = 8899
    env = dict(os.environ)
    env["CATALYST_MCP_HTTP"] = "1"
    env["CATALYST_MCP_PORT"] = str(port)
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        ready = False
        for _ in range(30):
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    ready = r.status == 200
                if ready:
                    break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)
        if not ready:
            return ["http: server did not become ready on " + base]

        def post(body, key=None):
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            req = urllib.request.Request(base + "/", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())

        init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
        if init.get("result", {}).get("protocolVersion") != "2025-06-18":
            fails.append(f"http initialize: bad response {init!r}")
        else:
            print("  ok  http initialize handshake")

        call = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "get_convergence_picks",
                                "arguments": {"limit": 2}}},
                    key=ikey)
        res = call.get("result", {})
        if res.get("isError") or not res.get("content"):
            fails.append(f"http tools/call: error -> {res}")
        else:
            print("  ok  http tools/call (intelligence key)")

        gated = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "get_ticker_signal",
                                 "arguments": {"ticker": "AAPL"}}})
        if not gated.get("result", {}).get("isError"):
            fails.append("http: free-tier gating not enforced")
        else:
            print("  ok  http free-tier gating")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return fails


def main() -> int:
    gaps = missing_inputs()
    if gaps:
        print("FAIL: missing inputs this test needs:")
        for g in gaps:
            print(f"  - {g}")
        print("\nThese are gitignored or live in the workspace root, so a bare\n"
              "clone of this repo cannot run the smoke test. Run it on a host\n"
              "with the data snapshots (e.g. the droplet at /opt/catalyst).")
        return 1

    failures = check_version_sync()
    for f in failures:
        print(f"  !!  {f}")
    if not failures:
        print("  ok  server.json / SERVER_VERSION in sync")

    tk = pick_ticker()
    ikey = intelligence_key()
    if not ikey:
        print("FAIL: no intelligence-tier key in mcp_keys.json")
        return 1

    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_convergence_picks", "arguments": {"limit": 5}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "get_ticker_signal", "arguments": {"ticker": tk}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "get_thesis", "arguments": {"ticker": tk}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "get_sector_lean", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "get_track_record", "arguments": {}}},
    ]
    resp = {r.get("id"): r for r in run(ikey, msgs)}

    init = resp.get(1, {}).get("result", {})
    if init.get("protocolVersion") != "2025-06-18":
        failures.append(f"initialize: bad protocolVersion {init!r}")
    else:
        print("  ok  initialize handshake")
    n_tools = len(resp.get(2, {}).get("result", {}).get("tools", []))
    if n_tools != len(EXPECTED_TOOLS):
        failures.append(f"tools/list: expected {len(EXPECTED_TOOLS)} tools, got {n_tools}")
    else:
        print(f"  ok  tools/list ({n_tools} tools)")
    served = {t["name"] for t in resp.get(2, {}).get("result", {}).get("tools", [])}
    if served != EXPECTED_TOOLS:
        failures.append(f"tools/list: name drift — missing {EXPECTED_TOOLS - served}, "
                        f"unexpected {served - EXPECTED_TOOLS}")
    for rid, label in [(3, "get_convergence_picks"), (4, "get_ticker_signal"),
                       (5, "get_thesis"), (6, "get_sector_lean"),
                       (7, "get_track_record")]:
        res = resp.get(rid, {}).get("result", {})
        if res.get("isError") or not res.get("content"):
            failures.append(f"{label}: error or empty -> {res}")
        else:
            print(f"  ok  {label}")

    # get_price_forecast only works once the Kronos pipeline has run.
    if KRONOS_FORECASTS.exists():
        fc = run(ikey, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "get_price_forecast",
                                    "arguments": {"limit": 3}}}])
        res = fc[0].get("result", {}) if fc else {}
        if res.get("isError") or not res.get("content"):
            failures.append(f"get_price_forecast: error or empty -> {res}")
        else:
            print("  ok  get_price_forecast")
    else:
        print("  --  get_price_forecast skipped (no kronos_forecasts.csv yet)")

    # Free tier: an intelligence-only tool must be gated.
    gated = run("", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "get_ticker_signal",
                                 "arguments": {"ticker": tk}}}])
    res = gated[0].get("result", {}) if gated else {}
    if not res.get("isError"):
        failures.append("free tier: get_ticker_signal should be tier-gated")
    else:
        print("  ok  free-tier gating (get_ticker_signal blocked)")

    print("  -- http transport --")
    failures.extend(test_http(ikey))

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
