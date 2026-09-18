# Catalyst Edge MCP Server

**Audited SEC catalyst intelligence for AI agents**, over the [Model Context Protocol](https://modelcontextprotocol.io). Every US-market SEC filing with market-moving potential is fetched from EDGAR, scored by 500+ data engines, and ranked before the market opens — and unlike most signal products, the track record is public: every past call is published with its outcome at [catalystedgescanner.com/receipts](https://catalystedgescanner.com/receipts/).

## Use the hosted server (no install)

A hosted instance is live. Point any MCP-capable client at it, or POST JSON-RPC directly:

```bash
curl -s https://catalystedgescanner.com/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Call a tool (no key needed for the free tier):

```bash
curl -s https://catalystedgescanner.com/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_convergence_picks","arguments":{"limit":3}}}'
```

Paid tiers use a Bearer token: `Authorization: Bearer <your-key>`.

## Tools

| Tool | Tier | Returns |
|---|---|---|
| `get_convergence_picks` | free | today's scored catalyst picks (free: top 3) |
| `get_track_record` | free | historical hit-rate / alpha of published picks |
| `get_ticker_signal` | intelligence | full per-ticker signal-layer breakdown |
| `get_thesis` | intelligence | plain-language thesis (catalysts, risks, bear case) |
| `get_sector_lean` | intelligence | directional sector lean |
| `get_options_context` | intelligence | nearest expiration, ATM call/put and straddle cost (live, via Tradier) |

**free** — evaluation tier, no key required. **intelligence** — all tools, full depth; keys via [catalystedgescanner.com/pricing](https://catalystedgescanner.com/pricing/) or catalystedgepro@gmail.com.

## Claude Desktop / Cursor config (stdio, self-hosted)

```json
{
  "mcpServers": {
    "catalyst-edge": {
      "command": "python3",
      "args": ["/path/to/catalyst_mcp.py"],
      "env": { "CATALYST_MCP_KEY": "optional-key" }
    }
  }
}
```

Stdlib Python only — no dependencies. HTTP mode: `CATALYST_MCP_HTTP=1 python3 catalyst_mcp.py` (binds `127.0.0.1:8848`; front with TLS for public use — see `catalyst-mcp.service`). Key→tier mapping lives in `mcp_keys.json` (see `mcp_keys.json.example`).

## Verify

```bash
python3 smoke_test.py   # exit 0 = handshake + all tools OK
```

The smoke test drives both transports, every tool, and free-tier gating. It
needs `mcp_keys.json` and the data snapshots the tools read, which are
gitignored or live in the workspace root — so a bare clone of this repo cannot
run it, and it will say which inputs are missing rather than crash. Run it
where the data lives.

## Operations

Deploying, log pulls, registry publishing and the scheduled health check are
one command each — see [docs/OPERATIONS.md](docs/OPERATIONS.md) and
[`scripts/`](scripts).

`server.json`'s `version` and `SERVER_VERSION` in `catalyst_mcp.py` must stay
in lockstep; the smoke test and the publish preflight both fail on a drift.

## Data & disclaimers

Sources: SEC EDGAR (filings, XBRL, insider activity), US government open data, delayed market prices. Nothing here is financial advice; signals are research with a published, audited track record — hits and misses both. Machine-readable site map: [catalystedgescanner.com/llms.txt](https://catalystedgescanner.com/llms.txt) · OpenAPI: [/openapi.json](https://catalystedgescanner.com/openapi.json).

MIT licensed.
