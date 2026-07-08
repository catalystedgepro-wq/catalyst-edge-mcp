#!/usr/bin/env python3
"""catalyst_mcp.py — Catalyst Edge MCP server, Phase 1 (intelligence tools).

Agent-native interface: a Model Context Protocol server speaking JSON-RPC 2.0
over stdio, exposing Catalyst Edge's catalyst intelligence as 5 read-only
tools an autonomous agent can call.

Stdlib only — no pip dependencies, consistent with the workspace convention.
The MCP wire protocol (initialize / tools/list / tools/call) is implemented
directly over newline-delimited JSON on stdin/stdout.

Scoped in docs/CATALYST_EDGE_MCP_SCOPE.md (Phase 1).

Tools:
  get_convergence_picks  — today's scored catalyst picks
  get_ticker_signal      — full per-ticker signal-layer breakdown
  get_thesis             — plain-language thesis for a ticker
  get_sector_lean        — orphan-aggregated sector lean
  get_track_record       — historical hit-rate / alpha (the trust signal)

Tiers: free (top-3 picks + track record) | intelligence (all tools, full).
Tier resolves from the CATALYST_MCP_KEY env var against mcp_keys.json;
an absent or unknown key resolves to the free tier.

Run (stdio): python3 mcp_server/catalyst_mcp.py
Run (HTTP):  CATALYST_MCP_HTTP=1 python3 mcp_server/catalyst_mcp.py   (or --http)
             Remote agents POST JSON-RPC to http://host:8848/ with a
             Bearer-token API key. Bind localhost; front with nginx TLS for
             public exposure to external agents / institutions.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "catalyst-edge"
SERVER_VERSION = "1.0.0"

_HERE = Path(__file__).resolve().parent
# Data root: env override, else the workspace root (parent of mcp_server/).
DATA_ROOT = Path(os.environ.get("CATALYST_DATA_ROOT", str(_HERE.parent)))
KEYS_FILE = _HERE / "mcp_keys.json"

TIER_RANK = {"free": 0, "intelligence": 1}

# Data-freshness guard. The upstream pipeline refreshes the CSV/JSON snapshots
# each trading morning before the open. If it stalls, the server must not keep
# presenting an old snapshot as "today's" — every data-backed response carries a
# staleness assessment so agents (and the site) can tell. Tunable per deploy;
# on weekends/holidays there is no fresh snapshot, so exceeding the threshold is
# expected and simply means "this is last-known, not today's."
try:
    STALE_AFTER_HOURS = float(os.environ.get("CATALYST_STALE_AFTER_HOURS") or 24)
except ValueError:
    STALE_AFTER_HOURS = 24.0


def log(msg: str) -> None:
    """Diagnostics go to stderr — stdout is the MCP protocol channel."""
    print(f"[catalyst_mcp] {msg}", file=sys.stderr, flush=True)


# ── data access ──────────────────────────────────────────────────────────────

def _read_csv(name: str) -> list[dict]:
    with (DATA_ROOT / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_json(rel: str) -> dict:
    return json.loads((DATA_ROOT / rel).read_text(encoding="utf-8"))


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _mtime_iso(name: str):
    try:
        ts = (DATA_ROOT / name).stat().st_mtime
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


def _parse_iso(as_of):
    """Parse an ISO-8601 timestamp (with offset or trailing 'Z') to an aware
    UTC datetime, or None if absent/unparseable."""
    if not as_of:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _freshness(as_of) -> dict:
    """Staleness assessment for a snapshot's `as_of` timestamp.

    Returns {stale, age_hours} and, when stale, a human-readable `note`. Stale
    means the snapshot is older than STALE_AFTER_HOURS — either the pre-market
    pipeline did not refresh (a real problem) or the market was closed
    (expected). Either way the caller learns the data is not today's instead of
    being handed it silently."""
    dt = _parse_iso(as_of)
    if dt is None:
        return {"stale": True, "age_hours": None,
                "note": "data timestamp unavailable — treat freshness as unknown"}
    age_h = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600.0
    age_h = round(age_h, 1)
    if age_h <= STALE_AFTER_HOURS:
        return {"stale": False, "age_hours": age_h}
    return {"stale": True, "age_hours": age_h,
            "note": (f"snapshot is {round(age_h / 24, 1)} day(s) old (as_of "
                     f"{as_of}); the pre-market pipeline may not have refreshed. "
                     f"Treat as last-known, not today's.")}


# ── Tradier options / market data ────────────────────────────────────────────

def _load_tradier_token() -> str:
    for path in ("/opt/catalyst/.env", str(_HERE.parent / ".env")):
        p = Path(path)
        if not p.exists():
            continue
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ln.startswith("TRADIER_TOKEN="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


TRADIER_TOKEN = _load_tradier_token()
TRADIER_BASE = "https://api.tradier.com/v1"


def _tradier_get(path: str) -> dict:
    req = urllib.request.Request(
        TRADIER_BASE + path,
        headers={"Authorization": f"Bearer {TRADIER_TOKEN}",
                 "Accept": "application/json",
                 "User-Agent": "CatalystEdge/1.0 (Python urllib)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── tier resolution ──────────────────────────────────────────────────────────

def _load_keys() -> dict:
    if not KEYS_FILE.exists():
        return {}
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8")).get("keys", {})
    except (OSError, json.JSONDecodeError):
        return {}


def tier_for_key(key: str) -> str:
    """Resolve an API key to a tier. Absent or unknown key -> 'free'.
    Called once per stdio session, or once per HTTP request."""
    key = (key or "").strip()
    if not key:
        return "free"
    return _load_keys().get(key, "free")


# ── tool handlers ────────────────────────────────────────────────────────────
# Each handler takes (args: dict, tier: str) and returns a JSON-able dict.
# Raise ToolError for an expected failure -> structured isError result.

class ToolError(Exception):
    pass


def tool_get_convergence_picks(args: dict, tier: str) -> dict:
    try:
        rows = _read_csv("convergence_alerts.csv")
    except OSError as e:
        raise ToolError(f"convergence data unavailable: {e}")
    conviction = (args.get("conviction") or "").strip().upper()
    sector = (args.get("sector") or "").strip().lower()
    if conviction:
        rows = [r for r in rows
                if (r.get("conviction_level") or "").upper() == conviction]
    if sector:
        rows = [r for r in rows if sector in (r.get("sector") or "").lower()]
    rows.sort(key=lambda r: _num(r.get("convergence_score")), reverse=True)
    try:
        requested = int(args.get("limit")) if args.get("limit") is not None else 25
    except (TypeError, ValueError):
        requested = 25
    limit = min(requested, 3) if tier == "free" else min(requested, 100)
    limit = max(1, limit)
    picks = [{
        "ticker": r.get("ticker"),
        "convergence_score": _num(r.get("convergence_score")),
        "conviction_level": r.get("conviction_level"),
        "signal_count": r.get("signal_count"),
        "signals_fired": r.get("signals_fired"),
        "sector": r.get("sector"),
    } for r in rows[:limit]]
    as_of = _mtime_iso("convergence_alerts.csv")
    out = {"as_of": as_of, "freshness": _freshness(as_of),
           "tier": tier, "count": len(picks), "picks": picks}
    if tier == "free":
        out["note"] = ("free tier returns the top 3. Full board + 4 more tools: "
                       "get a key at https://catalystedgescanner.com/pricing/ or "
                       "request a free developer key via POST "
                       "https://catalystedgescanner.com/api/v1/filing/signup")
    return out


def tool_get_ticker_signal(args: dict, tier: str) -> dict:
    ticker = (args.get("ticker") or "").strip().upper()
    if not ticker:
        raise ToolError("ticker is required")
    try:
        rows = _read_csv("convergence_alerts.csv")
    except OSError as e:
        raise ToolError(f"convergence data unavailable: {e}")
    row = next((r for r in rows
                if (r.get("ticker") or "").upper() == ticker), None)
    if not row:
        raise ToolError(
            f"ticker '{ticker}' is not in today's convergence universe")
    layers = {k: _num(v) for k, v in row.items()
              if k.endswith("_pts") and _num(v) != 0}
    as_of = _mtime_iso("convergence_alerts.csv")
    return {
        "as_of": as_of,
        "freshness": _freshness(as_of),
        "ticker": ticker,
        "convergence_score": _num(row.get("convergence_score")),
        "conviction_level": row.get("conviction_level"),
        "signal_count": row.get("signal_count"),
        "signals_fired": row.get("signals_fired"),
        "sector": row.get("sector"),
        "active_layers": layers,
    }


def tool_get_thesis(args: dict, tier: str) -> dict:
    ticker = (args.get("ticker") or "").strip().upper()
    if not ticker:
        raise ToolError("ticker is required")
    try:
        data = _read_json("docs/data/theses.json")
    except OSError as e:
        raise ToolError(f"thesis data unavailable: {e}")
    theses = data.get("theses") or {}
    thesis = theses.get(ticker)
    if not thesis:
        raise ToolError(
            f"no thesis for '{ticker}' — theses cover the top "
            f"{data.get('top_n', 'N')} convergence picks")
    as_of = data.get("generated_utc")
    return {"ticker": ticker, "as_of": as_of, "freshness": _freshness(as_of),
            "thesis": thesis}


def tool_get_sector_lean(args: dict, tier: str) -> dict:
    try:
        rows = _read_csv("orphan_sector_lean.csv")
    except OSError as e:
        raise ToolError(f"sector-lean data unavailable: {e}")
    sectors = [{
        "sector": r.get("sector"),
        "lean_pts": _num(r.get("lean_pts")),
        "raw_lean_uncapped": _num(r.get("raw_lean_uncapped")),
        "contributors_count": r.get("contributors_count"),
    } for r in rows]
    sectors.sort(key=lambda s: s["lean_pts"], reverse=True)
    as_of = _mtime_iso("orphan_sector_lean.csv")
    return {"as_of": as_of, "freshness": _freshness(as_of),
            "count": len(sectors), "sectors": sectors}


def tool_get_track_record(args: dict, tier: str) -> dict:
    try:
        rows = _read_csv("sec_outcome_summary.csv")
    except OSError as e:
        raise ToolError(f"track-record data unavailable: {e}")
    want = (args.get("list_name") or "").strip().lower()
    if want:
        rows = [r for r in rows if want in (r.get("list_name") or "").lower()]
    records = [{
        "list_name": r.get("list_name"),
        "rows": r.get("rows"),
        "wins": r.get("wins"),
        "losses": r.get("losses"),
        "hit_rate_2pct": _num(r.get("hit_rate_2pct")),
        "hit_rate_5pct": _num(r.get("hit_rate_5pct")),
        "avg_alpha_close_pct": _num(r.get("avg_alpha_close_pct")),
        "avg_realistic_pnl_net_pct": _num(r.get("avg_realistic_pnl_net_pct")),
        "cohort_90d_hit_rate_2pct": _num(r.get("cohort_90d_hit_rate_2pct")),
    } for r in rows]
    as_of = _mtime_iso("sec_outcome_summary.csv")
    return {"as_of": as_of, "freshness": _freshness(as_of),
            "count": len(records), "track_record": records}


def tool_get_options_context(args: dict, tier: str) -> dict:
    """Live options context for a ticker via Tradier — nearest expiration,
    ATM call/put, straddle cost. Lets an agent choose an options strategy."""
    ticker = (args.get("ticker") or "").strip().upper()
    if not ticker:
        raise ToolError("ticker is required")
    if not TRADIER_TOKEN:
        raise ToolError("options data unavailable — TRADIER_TOKEN not configured")
    try:
        exp = _tradier_get(f"/markets/options/expirations?symbol={ticker}")
        dates = (exp.get("expirations") or {}).get("date") or []
        if isinstance(dates, str):
            dates = [dates]
        if not dates:
            raise ToolError(f"no listed options for '{ticker}'")
        nearest = dates[0]
        q = _tradier_get(f"/markets/quotes?symbols={ticker}")
        spot = _num(((q.get("quotes") or {}).get("quote") or {}).get("last"))
        chain = _tradier_get(
            f"/markets/options/chains?symbol={ticker}&expiration={nearest}")
        opts = (chain.get("options") or {}).get("option") or []
        if isinstance(opts, dict):
            opts = [opts]
        atm = {"call": None, "put": None}
        for kind in ("call", "put"):
            legs = [o for o in opts if o.get("option_type") == kind
                    and _num(o.get("strike")) > 0]
            if legs and spot > 0:
                best = min(legs, key=lambda o: abs(_num(o.get("strike")) - spot))
                bid, ask = _num(best.get("bid")), _num(best.get("ask"))
                atm[kind] = {"strike": _num(best.get("strike")), "bid": bid,
                             "ask": ask, "mid": round((bid + ask) / 2, 4)}
        straddle = None
        if atm["call"] and atm["put"]:
            straddle = round(atm["call"]["mid"] + atm["put"]["mid"], 4)
        return {
            "ticker": ticker,
            "spot": spot,
            "nearest_expiration": nearest,
            "expirations_listed": len(dates),
            "atm_call": atm["call"],
            "atm_put": atm["put"],
            "atm_straddle_cost": straddle,
        }
    except ToolError:
        raise
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
        raise ToolError(f"Tradier options fetch failed: {type(e).__name__}: {e}")


# ── tool registry ────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_convergence_picks",
        "description": ("Today's top scored catalyst picks from the Catalyst "
                        "Edge convergence model. Optional filters: conviction, "
                        "sector. Free tier returns the top 3. Every response "
                        "carries a `freshness` block (as_of, age, stale flag) — "
                        "check it before treating the snapshot as today's."),
        "min_tier": "free",
        "handler": tool_get_convergence_picks,
        "inputSchema": {
            "type": "object",
            "properties": {
                "conviction": {"type": "string",
                               "enum": ["MAXIMUM", "HIGH", "ELEVATED", "WATCH"],
                               "description": "filter by conviction level"},
                "sector": {"type": "string",
                           "description": "filter by sector (substring match)"},
                "limit": {"type": "integer",
                          "description": "max picks (default 25; free tier capped at 3)"},
            },
        },
    },
    {
        "name": "get_ticker_signal",
        "description": ("Full convergence breakdown for one ticker — every "
                        "non-zero signal layer and its points, plus the score "
                        "and conviction."),
        "min_tier": "intelligence",
        "handler": tool_get_ticker_signal,
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string",
                                      "description": "ticker symbol"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_thesis",
        "description": ("Plain-language thesis for a ticker — quantitative "
                        "read, catalysts, risks, bear case. Covers the top "
                        "convergence picks."),
        "min_tier": "intelligence",
        "handler": tool_get_thesis,
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string",
                                      "description": "ticker symbol"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_sector_lean",
        "description": ("Orphan-aggregated sector lean — the directional "
                        "signal each sector is showing, from ~480 aggregated "
                        "data spokes."),
        "min_tier": "intelligence",
        "handler": tool_get_sector_lean,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_options_context",
        "description": ("Live options context for a ticker via Tradier — "
                        "nearest expiration, ATM call/put prices, straddle "
                        "cost. Use to choose an options strategy."),
        "min_tier": "intelligence",
        "handler": tool_get_options_context,
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string",
                                      "description": "ticker symbol"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_track_record",
        "description": ("Historical hit-rate and alpha of Catalyst Edge pick "
                        "lists — the evidence an agent uses to weight the "
                        "signal. Optional: list_name filter."),
        "min_tier": "free",
        "handler": tool_get_track_record,
        "inputSchema": {
            "type": "object",
            "properties": {"list_name": {
                "type": "string",
                "description": "filter to one pick list (substring match)"}},
        },
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def _public_tools() -> list[dict]:
    return [{"name": t["name"], "description": t["description"],
             "inputSchema": t["inputSchema"]} for t in TOOLS]


# ── JSON-RPC / MCP protocol ──────────────────────────────────────────────────

def handle_tools_call(params: dict, tier: str) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = TOOLS_BY_NAME.get(name)
    if not tool:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True}
    if TIER_RANK.get(tier, 0) < TIER_RANK.get(tool["min_tier"], 0):
        msg = (f"tool '{name}' requires the '{tool['min_tier']}' tier — "
               f"current tier is '{tier}'. Supply an intelligence-tier API "
               f"key (Bearer token over HTTP, or CATALYST_MCP_KEY for stdio).")
        return {"content": [{"type": "text", "text": msg}], "isError": True}
    try:
        result = tool["handler"](args, tier)
        return {"content": [{"type": "text",
                             "text": json.dumps(result, indent=2)}],
                "isError": False}
    except ToolError as e:
        return {"content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True}
    except Exception as e:  # noqa: BLE001 — never crash the server on a tool
        log(f"tool {name} crashed: {type(e).__name__}: {e}")
        return {"content": [{"type": "text",
                             "text": f"internal error in {name}"}],
                "isError": True}


def handle_message(msg: dict, tier: str):
    """Return a JSON-RPC response dict, or None for notifications.
    `tier` is resolved per stdio-session or per HTTP-request."""
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = msg_id is None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "notifications/initialized":
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _public_tools()}
    elif method == "tools/call":
        result = handle_tools_call(msg.get("params") or {}, tier)
    else:
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601,
                          "message": f"method not found: {method}"}}

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def run_stdio() -> int:
    """Local transport — one process per client; tier fixed for the session."""
    tier = tier_for_key(os.environ.get("CATALYST_MCP_KEY", ""))
    log(f"stdio transport — tier={tier} data_root={DATA_ROOT} tools={len(TOOLS)}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = handle_message(msg, tier)
        except Exception as e:  # noqa: BLE001
            log(f"dispatch error: {type(e).__name__}: {e}")
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": "internal error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


def run_http(host: str, port: int) -> int:
    """Network transport — remote MCP clients (agents, trading platforms,
    institutions) POST JSON-RPC with a Bearer-token API key. Tier is resolved
    per request. Bind to localhost and front with an nginx TLS proxy for
    public exposure."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass  # diagnostics go to stderr via log()

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") in ("", "/health"):
                self._send(200, {"status": "ok", "server": SERVER_NAME,
                                 "version": SERVER_VERSION})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            tier = tier_for_key(key)
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                msg = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700,
                                           "message": "parse error"}})
                return
            try:
                resp = handle_message(msg, tier)
            except Exception as e:  # noqa: BLE001
                log(f"http dispatch error: {type(e).__name__}: {e}")
                resp = {"jsonrpc": "2.0",
                        "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": "internal error"}}
            if resp is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(200, resp)

    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    log(f"HTTP transport on {host}:{port} — POST JSON-RPC, Bearer-key auth")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    if "--http" in sys.argv or os.environ.get("CATALYST_MCP_HTTP"):
        host = os.environ.get("CATALYST_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("CATALYST_MCP_PORT", "8848"))
        return run_http(host, port)
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
