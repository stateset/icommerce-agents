# stateset-icommerce-agents

[![CI](https://github.com/stateset/icommerce-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/stateset/icommerce-agents/actions/workflows/ci.yml)

Anthropic's `commerce-agents` architecture — a shopping agent and a merchant agent,
each on two paths (the Messages API host and a role MCP server) — running on the
StateSet iCommerce embedded engine (`stateset-embedded`) instead of an in-memory demo
backend. One fictional store, ACME Supply, one SQLite-backed engine instance, two agent
roles.

Upstream's guardrails sit in front of the model: fencing, provenance gates, caps,
staged writes, host approval. They stop a misbehaving model. The engine's policy kernel
sits underneath, refusing inside the database transaction and returning a sealed
receipt; it stops any caller, including one that never goes through an agent. Running
the two together is what this repo is for, and showing where the second layer stops is
what it is for.

## What it demonstrates

Two merchant writes — same operator, same approval, same apply path:

```
price update, TENT-RIDGE-TAN 219.00 -> 199.00
  evidence  kind=activity_log     id=46d07ac4-...

restock of a SKU with no inventory item
  evidence  kind=kernel_receipt   id=5e2eb8f8-...
```

The restock is a governed command, so the engine seals a receipt for it. The price
change is merchandising, which the engine does not govern, so the record is an
activity-log entry and the agent layer's approval gate is the only thing that refused
an unapproved apply. `web/portal` renders the two differently on purpose.

`scripts/denials.py` prints three refusals end to end, no API key required: a cart
write naming a product the model never saw (agent layer), an apply with no operator
approval (agent layer), and a refund of 10,000.00 against a 219.00 payment — refused by
the engine inside the transaction, `commerce.refund.exceeds_captured`, with a receipt
id. The first two protect you from the model. The third holds against anything.

`python scripts/run_demo.py --web --tour` produces this contrast live, with no API key:
it drives the checkout over the real routes, and both merchant applies go through the
same backend the host wires up — staging and applying a merchant change have no route
without a model. Both web apps render the result on load from `GET /shopping/orders`
and `GET /merchant/changes`.

## Layout

- `vendor/commerce-agents/` — the upstream repo, a git submodule, never edited.
- `engine_backend/` — `StorefrontBackend` and `MerchantBackend` implemented over the
  engine: `storefront.py`, `merchant.py`, `catalog.py`, `search.py`, `content.py`,
  `listings.py` (the family/variant resolution both roles share), `analysis.py` (the
  merchant's capped read-only `SELECT` surface), `staging.py`, `apply.py` (the five-kind
  apply dispatch, the only place live state is mutated), `custom_objects.py` (the one
  shape this repo stores in the engine's custom objects), `money.py`, `kernel.py`,
  `store.py`, `seed.py`. `docs/mapping.md` is the method-by-method map of what each read
  and write actually does. `scripts/check.py` fails if a module here is named in neither
  file.
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
- `evals/` — six graded cases checking rules the prompts, not the code, are relied on
  for (`docs/safety.md`'s "still asked of the model" list), each one's grader literals
  checked against the seeded store and the real serializers by `tests/test_evals.py`;
  run live against `claude-sonnet-5` on 2026-09-03 at 4/6 — see `evals/README.md` and
  `docs/testing.md` for the score and the two genuine model-behavior findings.
- `docs/` — `enforcement.md` (what is governed and what is not, and by which layer),
  `mapping.md` (the backend method map, the pinned submodule commit, the SQL
  fallbacks), `install.md`, `mcp.md` (connecting an MCP client, and its weaker
  approval guarantee), `testing.md` (what the suite covers and what it does not).
- `tests/` — one file per module above, run with `pytest`.
- `.github/workflows/` — CI: a Python job (ruff, pytest, the drift check, the denial
  walkthrough, the keyless tour run twice against the same db) and a Node 22 job
  (`npm audit --audit-level=high`, the two web builds, then a headless Chromium check
  that runs the tour against a live host and asserts the portal and storefront
  actually render it); see its own `README.md`.

## Run it

```bash
python scripts/run_demo.py            # FastAPI host on :8000
python scripts/run_demo.py --web      # also starts web/storefront (:3000) and web/portal (:3100)
python scripts/run_demo.py --web --tour  # ...and runs scripts/tour.py against it, no API key needed
python scripts/denials.py             # three refusals, printed end to end, no API key needed
python scripts/smoke_chat.py          # one live conversation per role; needs ANTHROPIC_API_KEY, else skips
python -m evals.run                   # the eval suite; needs ANTHROPIC_API_KEY, else skips
```

`docs/install.md` has the Python version, submodule, and glibc-wheel details.
`web/storefront` and `web/portal` are Next.js 16 / React 19 and need **Node >= 20.9**
(`nvm use 22`). A live chat turn (`/shopping/chat`, `/merchant/chat`, either web app,
`smoke_chat.py`, or `evals/`) needs `ANTHROPIC_API_KEY` in the environment; everything
else, including `denials.py` and the full test suite, does not. An identity-linked key
also needs `ANTHROPIC_WORKSPACE_ID` set — without it the request fails with a 400 naming
the `anthropic-workspace-id` header; an unlinked key ignores the variable. `docs/testing.md` is
the honest account of what the test suite proves and what it does not — read it before
trusting a green CI run to mean more than "the code, not the agent's behavior, is
correct."

## Where the interfaces are

- `POST /shopping/session`, `POST /shopping/chat`, `POST /shopping/cart/add`,
  `POST /shopping/checkout`, `GET /shopping/cart`, `GET /shopping/orders`,
  `POST /merchant/session`, `POST /merchant/chat`, `POST /merchant/changes/{id}/approve`,
  `GET /merchant/changes`, `GET /capabilities`, `GET /healthz` — `host/app.py`. The `GET`
  routes are reads (`GET /capabilities` excepted) behind the same 401 gate as every
  other route — `GET /shopping/cart` and `GET /merchant/changes` are session-scoped,
  while `GET /shopping/orders` is customer-scoped, so any session bound to the seeded
  customer sees that customer's orders — and both web apps use them to render live
  store state.
- The MCP tool surface — 13 shopping tools, 19 merchant tools — `mcp_servers/`, wired
  up in `docs/mcp.md`.
- The governed kernel seam — `engine_backend/kernel.py`'s `KernelClient.execute`,
  the only place a command reaches the engine's own policy check.

## Enforcement

Every write in this repo goes through the agent layer's gates (`shopping_agent.gates`,
`merchant_agent.gates`). Only the commands this deployment's kernel policy governs are
*also* checked by the engine's own kernel, and no merchandising write is one of them.

The engine governs 26 of its 474 mutations, and they are the transaction spine.
This deployment enables five of those 26 in `config/kernel-policy.json`; two of the five
have no code path here, which `docs/enforcement.md` names rather than leaves implied.
That document is the full account — which layer stops which write, what evidence comes
back, and the reason a merchant agent editing listings and prices has its agent layer
and nothing beneath it.

The one guarantee enforced twice is approval: upstream's `require_host_approval` at the
agent layer, and `requires_approval` on `payments.create_refund` in the kernel policy.
The kernel's copy holds even when the agent layer is bypassed entirely.

## Verify

```bash
ruff check . && ruff format --check . && pytest && python scripts/check.py
```
