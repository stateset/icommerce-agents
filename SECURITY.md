# Security policy

## Supported versions

Security fixes are provided for the latest tagged minor release. Pre-`1.0` releases may
include breaking changes; after `1.0`, the latest minor release on the current major is
supported. Deployment-specific forks remain the operator's responsibility.

| Version | Supported |
|---|---|
| 0.9.x | Yes |
| < 0.9 | No |

## Reporting a vulnerability

Do not open a public issue. Use GitHub's **Security → Report a vulnerability** private
reporting flow for `stateset/icommerce-agents`. Include the affected revision, impact,
reproduction, and any suggested mitigation. Do not include customer data, credentials,
private keys, payment signatures, or production wallet details.

Maintainers will acknowledge the report, assess severity, and coordinate disclosure
after a fix is available. No response-time SLA or bug bounty is implied by this policy.

## Deployment boundary

This repository is a reference implementation, not a managed payment processor. The
operator owns identity-provider configuration, TLS and ingress limits, host and volume
security, secret management, stablecoin treasury controls, sanctions/tax/consumer-law
obligations, monitoring, backups, and incident response. Never place payer or treasury
private keys in this service.
