# Connecting an MCP client

`mcp_servers/shopping.py` and `mcp_servers/merchant.py` expose the same 13- and 18-tool
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

Every variable each server reads is collected in `mcp_servers/settings.py` and validated
once at startup; a bad port or an empty principal fails before the store is opened.

| Shopping server | Merchant server | Default | Meaning |
|---|---|---|---|
| `STOREFRONT_MCP_DB` | `MERCHANT_MCP_DB` | `data/acme.db` | Engine store file (never `:memory:`) |
| `ACME_CUSTOMER` | `ACME_OPERATOR` | seeded principal | The one principal the process acts for |
| `STOREFRONT_MCP_SESSION_ID` | `MERCHANT_MCP_SESSION_ID` | `mcp-shopping` / `mcp-merchant` | Durable session handle bound to that principal |
| `STOREFRONT_MCP_HOST` | `MERCHANT_MCP_HOST` | `127.0.0.1` | Bind address; off-loopback needs the unsafe flag below |
| `STOREFRONT_MCP_PORT` | `MERCHANT_MCP_PORT` | `8300` / `8301` | Bind port |
| `STOREFRONT_MCP_MEMORY_FILE` | `MERCHANT_MCP_MEMORY_FILE` | repo-local JSON | Customer or store memory file |
| `STOREFRONT_MCP_UNSAFE_ALLOW_NO_AUTH` | `MERCHANT_MCP_UNSAFE_ALLOW_NO_AUTH` | unset | Permit a non-loopback bind |

For a client that speaks HTTP directly rather than spawning the process itself, run
`python -m mcp_servers.shopping` / `python -m mcp_servers.merchant` and point the client
at `http://127.0.0.1:8300/mcp` / `http://127.0.0.1:8301/mcp` (ports configurable via
`STOREFRONT_MCP_PORT` / `MERCHANT_MCP_PORT`). Both refuse to bind off loopback unless
`STOREFRONT_MCP_UNSAFE_ALLOW_NO_AUTH=1` / `MERCHANT_MCP_UNSAFE_ALLOW_NO_AUTH=1` is set —
this reference server has no authentication of its own, so that variable exists to be
set only once an authenticating gateway is actually in front of it.

## Approval is outside the model's tool surface

`stage_*` tools only record a proposed change for preview. The merchant server exposes
no approval tool. `apply_change` refuses a `change_id` until a trusted operator surface
has recorded approval in the shared durable ledger. With the host and MCP server pointed
at the same database, the supported flow is:

1. Claude stages through MCP.
2. An operator reviews the exact diff and its SHA-256 proposal digest in the portal.
3. The separately authenticated host route `POST /merchant/changes/{id}/approve`
   receives that displayed digest and records approval only if it still matches the
   immutable proposal.
4. Claude may call `apply_change`; the backend atomically consumes that approval.

This separation is enforced by capability design, not by hoping the model declines to
self-approve. The ledger binds the digest and operator, permits only one process to
spend an approval, and leases every affected target. A post-dispatch ambiguity remains
blocked until the portal compares observed state with the approved proposal and an
operator explicitly reconciles it.

## Deployment boundary

The reference MCP processes deliberately bind only to loopback by default and carry a
single process-level principal from the environment. They are suitable for a local
desktop client or for a dedicated process behind an authenticating gateway; they are
not a multi-tenant public endpoint. For remote deployment, terminate MCP authorization
at a gateway implementing the
[MCP authorization specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization),
start a principal-scoped
backend, and leave the unsafe off-loopback flag unset unless that boundary is actually
present. The FastAPI host's production JWT mode authenticates its own HTTP approval
surface; it does not silently make a separately exposed MCP port authenticated.

The removed `host_approve` tool is documented in `docs/testing.md` as a historical live
finding: one model correctly declined to call it and another called it unprompted. That
variance is exactly why it is no longer a model-callable capability.
