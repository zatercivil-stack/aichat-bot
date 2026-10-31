# Security

- Secrets only via environment variables; never commit `.env`.
- Admin auth uses strong tokens, rate limiting, CSRF (HMAC), secure cookies.
- Secret comparisons use `secrets.compare_digest`.
- Webhook verified server-side against YooKassa API.
- No real secrets in logs.
