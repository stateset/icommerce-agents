# Connecting an MCP client

`mcp_servers/shopping.py` and `mcp_servers/merchant.py` expose the same 13- and 19-tool
role surface the Messages API host uses — `search_products`,
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

## Approval is out-of-band; MCP cannot approve

`stage_*` tools only record a proposed change for preview. `apply_change` is the only
call that mutates live state, and it refuses any `change_id` that has not first been
approved via the host's HTTP route:

- `POST /merchant/changes/{id}/approve` (operator identity comes from the session,
  never a tool argument or request-body field).

There is no MCP tool that records approval. The model cannot self-approve; a human
operator uses the portal (or equivalent HTTP) first, then `apply_change` can succeed.
`EngineMerchant.apply_change` checks its own backend `approved_ids` independently of
anything the MCP handler does.

## Approval guarantee on MCP matches the host

The FastAPI host's approval path is out-of-band by construction: only the operator's own
browser session can reach `POST /merchant/changes/{id}/approve`, and the operator is
read from the session binding. The merchant MCP server mirrors that guarantee: there is
no approval tool on the MCP surface, so the model cannot approve on its own. A change
can be applied only after the host has recorded an approval for its `change_id`.
