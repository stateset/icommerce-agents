# Support and compatibility

## Supported reference envelope

- Python 3.12 on Linux with glibc 2.34 or newer.
- Node.js 22 for the two Next.js applications.
- `stateset-embedded==1.28.5` and the exact `vendor/commerce-agents` submodule commit
  recorded in `docs/mapping.md`.
- One durable SQLite database on a single host-local filesystem, with one or more host
  worker processes on that machine. Network filesystems and active-active multi-host
  writers are unsupported.
- EVM x402 `exact` stablecoin checkout only on a deployment-reviewed network, ERC-20
  token, facilitator, wallet set, and recipient.
- Stablecoin refunds through the documented `stablecoin-refund-v1` HTTPS treasury
  adapter, after deployment-specific signing and reconciliation controls are reviewed.

## Compatibility promise

Before `1.0`, minor releases may change HTTP payloads, environment variables, adapter
tables, and operational procedures. A future `1.x` release will preserve documented
HTTP contracts and stored-data upgrades within the major version; removals require a
deprecation notice and migration path. The vendored Anthropic contracts and embedded
engine version remain explicit compatibility pins rather than floating dependencies.

## Explicit exclusions

The reference does not supply a merchant's tax determination, carrier/warehouse,
returns, sanctions screening, treasury/off-ramp, custodial signer, or identity provider.
Those systems must be selected, integrated, and evidenced for the deployment.
Stablecoin refunds are not production-supported until the configured treasury adapter's
transfer and reconciliation workflow passes the release gate.

Use GitHub issues for reproducible, non-sensitive defects and discussions. Use the
private process in `SECURITY.md` for vulnerabilities.
