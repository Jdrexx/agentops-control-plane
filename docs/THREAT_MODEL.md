# Threat model

## Trust modes

Default local mode assumes a trusted operator and loopback-only exposure. Any network-exposed mode requires `AGENTOPS_API_KEY`, `AGENTOPS_ENCRYPTION_KEY`, TLS at a trusted reverse proxy, and a PostgreSQL database or persistent SQLite volume.

## Controls implemented

- Bound SQL parameters and foreign keys
- Strict Pydantic sizes, enums, identifiers, and workflow tool allowlists
- Optional bearer authentication with admin, operator, and viewer roles
- SHA-256 token hashes and constant-time token comparisons
- Fernet authenticated encryption for stored project secrets
- Recursive credential-field redaction in traces, memory, webhooks, and exports
- Per-actor request throttling, CSP, frame denial, MIME-sniff protection, and referrer restrictions
- Append-only mutation audit records
- Single-use approvals with edit, expiry, rejection, and escalation states
- Explicit tool allowlists, step budgets, handoff project isolation, and recursive-loop detection
- HTTP/HTTPS scheme validation and bounded timeouts for provider, webhook, and OTLP calls

## Residual risks

- Workflow inputs remain plaintext in operational tables so interrupted runs and exact replay work. Do not place raw credentials in workflow input; store them in the encrypted secrets API and pass only references.
- The built-in worker executes trusted adapters in-process. New shell, browser, or arbitrary-code tools require a separate sandboxed worker with an explicit capability policy.
- Outbound webhook URLs are administrator-controlled but can reach network-visible HTTP services. Hosted deployments should enforce an outbound proxy or network allowlist.
- The local scheduler and executor are single-instance. Multi-replica deployment requires distributed leases or a queue to prevent duplicate scheduled dispatch.
- Rate limits are per process. Use an edge or shared limiter for multi-replica deployments.
- The dashboard stores its bearer token in session storage. CSP and output escaping reduce XSS risk; hardened deployments should use short-lived credentials.

## Operational requirements

- Terminate TLS at a trusted reverse proxy before allowing network access.
- Use independent random values for API and encryption keys.
- Restrict provider keys to the minimum provider permissions and budgets.
- Back up the database and encryption key separately.
- Review `/api/audit`, alerts, failed webhook deliveries, and provider spend.
- Rotate credentials after suspected disclosure; existing encrypted secrets must be re-encrypted before changing the encryption key.
