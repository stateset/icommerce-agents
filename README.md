# stateset-icommerce-agents

Anthropic's `commerce-agents` architecture — a shopping agent and a merchant agent,
each on two paths (the Messages API host and a role MCP server) — running on the
StateSet iCommerce embedded engine (`stateset-embedded`) instead of an in-memory demo
backend. One fictional store, ACME Supply, one SQLite-backed engine instance, two agent
roles.

## Layout

- `vendor/commerce-agents/` — the upstream repo, a git submodule, never edited.
- `engine_backend/` — `StorefrontBackend` and `MerchantBackend` implemented over the
  engine: `storefront.py`, `merchant.py`, `catalog.py`, `search.py`, `content.py`,
  `staging.py`, `kernel.py`, `store.py`, `seed.py`. `docs/mapping.md` is the method-by-
  method map of what each read and write actually does.
- `config/` — `kernel-policy.json` and `kernel-principal.json`, the host-owned files
  the kernel checks every governed command against; never model input.
- `host/` — the FastAPI host: one engine store, both agents, session binding, the
  checkout route, and the merchant approval route.
- `mcp_servers/` — `shopping.py` and `merchant.py`, the two MCP servers over the same
  backends and gates as the host.
- `web/storefront/` and `web/portal/` — Next.js chat UIs against the host's
  `/shopping/*` and `/merchant/*` routes.
- `scripts/` — `run_demo.py` (starts the host, and with `--web` the two web apps),
  `denials.py` (three refusals, end to end), `check.py` (the drift check), `install.sh`.
- `docs/` — `enforcement.md` (what is governed and what is not, and by which layer),
  `mapping.md` (the backend method map, the pinned submodule commit, the SQL
  fallbacks), `install.md`, `mcp.md` (connecting an MCP client, and its weaker
  approval guarantee).
- `tests/` — one file per module above, run with `pytest`.

## Run it

```bash
python scripts/run_demo.py            # FastAPI host on :8000
python scripts/run_demo.py --web      # also starts web/storefront (:3000) and web/portal (:3100)
python scripts/denials.py             # three refusals, printed end to end, no API key needed
```

`docs/install.md` has the Python version, submodule, and glibc-wheel details. A live
chat turn (`/shopping/chat`, `/merchant/chat`, or either web app) needs
`ANTHROPIC_API_KEY` in the environment; everything else, including `denials.py` and the
full test suite, does not.

## Where the interfaces are

- `POST /shopping/session`, `POST /shopping/chat`, `POST /shopping/cart/add`,
  `POST /shopping/checkout`, `POST /merchant/session`, `POST /merchant/chat`,
  `POST /merchant/changes/{id}/approve`, `GET /healthz` — `host/app.py`.
- The MCP tool surface — 13 shopping tools, 19 merchant tools — `mcp_servers/`, wired
  up in `docs/mcp.md`.
- The governed kernel seam — `engine_backend/kernel.py`'s `KernelClient.execute`,
  the only place a command reaches the engine's own policy check.

## Enforcement

Every write in this repo goes through the agent layer's gates (`shopping_agent.gates`,
`merchant_agent.gates`). Only the five commands this deployment's kernel policy governs
are *also* checked by a second, independent layer — the engine's own kernel — and no
merchandising write is one of them. `docs/enforcement.md` is the full
account, including the finding it exists to state: the engine governs the transaction
spine — checkout, payments, refunds, order and reservation transitions, the stock
ledger — and does not govern merchandising, where the agent layer's guardrails and
approval gate are the only defense. `scripts/denials.py` demonstrates one refusal from
each side plus the doubled case.

## Verify

```bash
ruff check . && ruff format --check . && pytest && python scripts/check.py
```
