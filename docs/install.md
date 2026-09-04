# Install

Python 3.12 exactly (`requires-python = ">=3.12,<3.13"` in `pyproject.toml` — the pinned
`stateset-embedded==1.28.5` wheel is built for 3.12). Node is needed only for `web/`.

```bash
git clone --recurse-submodules <this repo>
cd stateset-icommerce-agents
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If the submodule was not fetched with the clone:

```bash
git submodule update --init
```

## The glibc caveat

`stateset-embedded` publishes prebuilt wheels tagged `manylinux_2_34`. On a system with
an older glibc (before 2.34 — check with `ldd --version`), pip cannot use the wheel and
falls back to building the sdist from source with `cargo` and `maturin`, which takes
minutes rather than seconds and requires a Rust toolchain on the machine doing the
install. This is expected, not a failure; let it run.

## Node

`web/storefront` and `web/portal` are Next.js 16 / React 19, matching the vendor
`web-shared` examples. Next 16 requires **Node ≥ 20.9**; this repo's install is
verified against Node 22.18.0. Select it (e.g. `nvm use 22`) before installing.

```bash
nvm use 22   # or any Node >= 20.9
npm install
npm run build --workspace web/storefront
npm run build --workspace web/portal
```

## Verify

```bash
ruff check . && ruff format --check . && pytest && python scripts/check.py
```

`scripts/denials.py` needs no `ANTHROPIC_API_KEY` — it exercises the agent-layer gates
and the kernel directly, not a model call:

```bash
python scripts/denials.py
```

Running the host for a live chat turn does need
`ANTHROPIC_API_KEY` set in the environment. An identity-linked key also needs
`ANTHROPIC_WORKSPACE_ID` set — without it, a request with such a key fails with a 400
naming the `anthropic-workspace-id` header; an unlinked key ignores the variable.

## Production HTTP authentication

The default `demo` mode uses the seeded Rowan customer and ACME operator so the local
tour remains keyless. Do not expose that mode publicly. Set these variables to make all
`/shopping/*` and `/merchant/*` routes require a verified bearer token:

```bash
export ICOMMERCE_AUTH_MODE=jwt
export ICOMMERCE_JWT_ISSUER=https://identity.example.com/
export ICOMMERCE_JWT_AUDIENCE=icommerce-host
export ICOMMERCE_JWKS_URL=https://identity.example.com/.well-known/jwks.json
export ICOMMERCE_ALLOWED_ORIGINS=https://shop.example.com,https://merchant.example.com
# In-process session lifetime; default eight hours, minimum one minute.
export ICOMMERCE_SESSION_TTL_SECONDS=28800
# Enables GET /metrics; use a separate, high-entropy monitoring credential.
export ICOMMERCE_METRICS_TOKEN=replace-with-32-plus-byte-monitoring-secret
# Earliest an operator may declare an abandoned applying claim; default 900 seconds.
export ICOMMERCE_STALE_APPLY_SECONDS=900
```

Customer tokens need role `customer` or scope `shopping:use` plus an `email` claim that
matches a provisioned engine customer. Merchant tokens need role `merchant` or scope
`merchant:write` plus `store_id` equal to this host's store. Every later request must
present both the bearer token and its `X-Session-Id`; the session is bound to the signed
`sub`, so possession of a leaked session id alone grants nothing. Public deployments
should use asymmetric JWKS verification. `ICOMMERCE_JWT_HS256_SECRET` exists for tests
and controlled private deployments, must contain at least 32 bytes, and is mutually
exclusive with `ICOMMERCE_JWKS_URL`. The JWKS URL must use HTTPS, include a hostname,
and contain no embedded username or password.

In JWT mode, `POST /shopping/checkout` also requires a validated `shipping_address`
object; it never substitutes the fictional demo address. The no-body form remains
available only in demo mode so the keyless tour stays frictionless.

Every normal response carries `X-Request-Id`, `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and
`X-Frame-Options: DENY`. A caller may supply a correlation id containing only ASCII
letters, digits, `.`, `_`, `:`, or `-` (up to 128 characters); malformed values are
replaced rather than reflected. Logs contain this id, method, route, status, and elapsed
time, but never the bearer token, session id, or request body.

`GET /metrics` is disabled with a 404 unless `ICOMMERCE_METRICS_TOKEN` is configured.
When enabled it requires that value (at least 32 bytes) as a bearer token and exports low-cardinality
Prometheus counters for route templates, response-policy rewrites, and governed refund
outcomes. It never labels a metric with a session, customer, operator, change, payment,
or request id.

This authenticates the FastAPI host, not the two separately launched MCP ports. See
`docs/mcp.md` before exposing either MCP server beyond loopback.

The host keeps chat state, principal bindings, and cart-to-session mappings in process.
Bindings expire after `ICOMMERCE_SESSION_TTL_SECONDS`, even if a client retains the id.
Run one worker, or use sticky routing that keeps a session on the worker that created it;
the approval ledger and target leases themselves are durable and cross-process safe.

The separate MCP servers remain loopback-only, principal-scoped processes rather than
public multi-tenant services. Put an MCP-spec authorization gateway in front of them
before deliberately enabling an off-loopback bind; host JWT mode does not authenticate
those ports.
