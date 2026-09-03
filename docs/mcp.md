# Connecting an MCP client

`mcp_servers/shopping.py` and `mcp_servers/merchant.py` expose the same ~15-20-tool
role surface the Messages API host and the Agent SDK console use — `search_products`,
cart and order tools, `search_policies`, and `get_fulfillment_options` on the shopping
side; business metrics, listings, the staged-change queue, and `apply_change` on the
merchant side — through the same executor and gates as those paths. They are not the
StateSet iCommerce engine's own MCP server, which exposes 900+ raw engine operations
behind a `--apply` flag. Connect an MCP client (Claude Code, Claude Desktop, Cursor) to
these two servers to drive the identical store through the identical role surface.

## Connecting

Both servers run over `streamable-http` and read identity from the environment — never
from a tool argument or from the client.

```json
{
  "mcpServers": {
    "acme-storefront": {
      "command": "/path/to/stateset-icommerce-agents/.venv/bin/python",
      "args": ["-m", "mcp_servers.shopping"],
      "env": {
        "ACME_CUSTOMER": "rowan@example.invalid",
        "STOREFRONT_MCP_DB": "/path/to/stateset-icommerce-agents/acme.db"
      }
    },
    "acme-merchant-back-office": {
      "command": "/path/to/stateset-icommerce-agents/.venv/bin/python",
      "args": ["-m", "mcp_servers.merchant"],
      "env": {
        "ACME_OPERATOR": "user:acme-operator",
        "MERCHANT_MCP_DB": "/path/to/stateset-icommerce-agents/acme.db"
      }
    }
  }
}
```

For a client that speaks HTTP directly rather than spawning the process itself, run
`python -m mcp_servers.shopping` / `python -m mcp_servers.merchant` and point the client
at `http://127.0.0.1:8300/mcp` / `http://127.0.0.1:8301/mcp` (ports configurable via
`STOREFRONT_MCP_PORT` / `MERCHANT_MCP_PORT`). Both refuse to bind off loopback unless
`STOREFRONT_MCP_UNSAFE_ALLOW_NO_AUTH=1` / `MERCHANT_MCP_UNSAFE_ALLOW_NO_AUTH=1` is set —
this reference server has no authentication of its own, so that variable exists to be
set only once an authenticating gateway is actually in front of it.

## Applying a merchant change takes two separate tool calls

`stage_*` tools only record a proposed change for preview. `apply_change` refuses any
`change_id` that has not first been marked by a separate `host_approve` tool call —
staging, approving, and applying a change are always three distinct tool calls, so a
client that surfaces each one to its user (the default in Claude Code, Claude Desktop,
and Cursor) gives the operator two independent, visible decision points: one before
`host_approve` runs, a second before `apply_change` runs.

`host_approve` marks nothing but its own `change_id` — it does no write of its own — and
`EngineMerchant.apply_change` re-checks that mark independently, so a change that
reaches `apply_change` through some other path (skipping `host_approve`) still cannot be
applied.

## Limitation: this depends on the connecting client, not on this server

**This is weaker than the Messages API host's approval surface.** The FastAPI host's
`POST /merchant/changes/{id}/approve` is a route only the operator's own browser session
can reach — out-of-band by construction, entirely outside the model's or any MCP
client's discretion. Here, `host_approve` and `apply_change` are both ordinary tools
sitting behind whatever the connecting MCP client does with a tool call.

**If your MCP client is configured to auto-approve tool calls** — skipping its own
confirmation prompts — nothing in this repository detects or refuses that, and a model
can stage, approve, and apply a merchant change unattended. That defeats the guarantee
this staging design otherwise provides. Do not run these servers unattended, and prefer
the Messages API host's HTTP approval route (`host/app.py`) over the MCP path for any
deployment where an operator's own out-of-band confirmation must be guaranteed rather
than merely conventional.
