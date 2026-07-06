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

### Data freshness

Every data-backed response carries a `freshness` block so consumers never
mistake a stale snapshot for today's:

```json
"as_of": "2026-07-06T12:30:00+00:00",
"freshness": { "stale": false, "age_hours": 2.1 }
```

The snapshots are refreshed each trading morning before the open. If the
pipeline stalls (or the market is closed), `freshness.stale` flips to `true`
with an explanatory `note` and `age_hours`, e.g. a Friday snapshot read the
following Monday. The threshold defaults to 24h and is tunable per deploy with
`CATALYST_STALE_AFTER_HOURS`.

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

## Data & disclaimers

Sources: SEC EDGAR (filings, XBRL, insider activity), US government open data, delayed market prices. Nothing here is financial advice; signals are research with a published, audited track record — hits and misses both. Machine-readable site map: [catalystedgescanner.com/llms.txt](https://catalystedgescanner.com/llms.txt) · OpenAPI: [/openapi.json](https://catalystedgescanner.com/openapi.json).

MIT licensed.
