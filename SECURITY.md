# Security Policy

## Reporting a vulnerability

Report security issues privately. Do not open a public issue.

Use GitHub's [private vulnerability reporting](https://github.com/selfhosthub/studio-console/security/advisories/new) on this repository.

Include what you found, how to reproduce it, and the console version (`studio-console version`).

## Scope

Console writes and reads operator secrets: `.env` files (mode 0600), `POSTGRES_PASSWORD`, `SHS_CREDENTIAL_ENCRYPTION_KEY`, `CLOUDFLARE_API_TOKEN`, and the Plus entitlement token. It also provisions DNS, TLS, tunnels, and access policies. Findings that expose any of these, or that allow an unprivileged local user to read them, are in scope.

Vulnerabilities in the Studio platform itself belong in the [studio](https://github.com/selfhosthub/studio) repository.
