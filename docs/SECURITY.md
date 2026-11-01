# Security

- Secrets are supplied through environment variables; `.env` and runtime data are excluded from Git and Docker build context.
- Production startup rejects weak admin secrets. Admin sessions use HttpOnly, Secure, SameSite cookies, constant-time token comparisons, login rate limiting and HMAC CSRF checks on admin POST requests.
- Serve the admin panel through an HTTPS reverse proxy. The Compose application port binds to loopback; configure trusted proxy handling to match your deployment.
- Automatic YooKassa payment activation verifies provider status, local payment ID, amount and currency through the server-side API. An absent currency is rejected.
- Explicit admin recovery commands can activate pending purchases manually; they share the idempotent transaction and retain existing provider identifiers.
- Payment and user row locks serialize activation and balance changes. External notifications and referral cashback after commit remain best-effort; see the case study for limits.
- Tests use explicit `ENV=test` and a database ending in `_test`; the standalone test Compose file has no production credentials, data mounts or published ports. Do not execute tests inside the production bot container.
- User photos and chat histories may be personal data. A deployment needs an explicit retention/deletion policy, access controls and provider configuration appropriate for its use. This repository does not prove that those operational policies are in place.

When reporting a defect in a public issue, include a minimal redacted reproduction without credentials or customer data.
