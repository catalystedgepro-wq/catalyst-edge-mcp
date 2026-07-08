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

## Freshness monitoring

`GET /health` reports data freshness alongside liveness: `data_as_of`,
`data_age_hours`, and `status` flips from `ok` to `stale` when the primary
snapshot is older than `CATALYST_STALE_AFTER_HOURS` (default 100h — the
longest healthy gap is a 3-day holiday weekend). Point an uptime monitor at
`/health` and alert on the string `"stale"` to catch a stopped pipeline the
morning it happens. Tool responses carry the same `stale` flag + warning so
agents never act on old picks silently.

## Data & disclaimers

Sources: SEC EDGAR (filings, XBRL, insider activity), US government open data, delayed market prices. Nothing here is financial advice; signals are research with a published, audited track record — hits and misses both. Machine-readable site map: [catalystedgescanner.com/llms.txt](https://catalystedgescanner.com/llms.txt) · OpenAPI: [/openapi.json](https://catalystedgescanner.com/openapi.json).

MIT licensed.
