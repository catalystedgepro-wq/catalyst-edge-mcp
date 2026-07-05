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
    keys = json.loads((HERE / "mcp_keys.json").read_text()).get("keys", {})
    for k, tier in keys.items():
        if tier == "intelligence":
            return k
    return ""


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
    tk = pick_ticker()
    ikey = intelligence_key()
    failures = []
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
    if n_tools != 6:
        failures.append(f"tools/list: expected 6 tools, got {n_tools}")
    else:
        print("  ok  tools/list (6 tools)")
    for rid, label in [(3, "get_convergence_picks"), (4, "get_ticker_signal"),
                       (5, "get_thesis"), (6, "get_sector_lean"),
                       (7, "get_track_record")]:
        res = resp.get(rid, {}).get("result", {})
        if res.get("isError") or not res.get("content"):
            failures.append(f"{label}: error or empty -> {res}")
        else:
            print(f"  ok  {label}")

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
