# Security Policy

## Supported versions

Only the latest release on the default branch receives security fixes. If you run a fork, keep
it in sync with upstream security commits.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting channel: open the repository's **Security** tab and choose
**Report a vulnerability** (GitHub Security Advisories). If that is unavailable, contact the
maintainers at **vanyzhang@qq.com** and ask for a private channel.

Please include:

- affected version or commit,
- a minimal reproduction (request, configuration, or script),
- impact assessment (what an attacker gains),
- any suggested fix.

You can expect an initial acknowledgement within 5 working days. We will keep you informed about
the assessment and the fix, and we will credit you in the advisory unless you prefer otherwise.

## Scope

In scope:

- authentication and authorization bypass (JWT, roles, department visibility, admin-only scope),
- secret exposure (export packages, MCP credentials, logs, audit records),
- SSRF and egress-control bypass through model or MCP endpoints,
- injection in agent, skill or knowledge ingestion paths,
- privilege escalation through import approval flows.

Out of scope:

- findings that require a already-compromised administrator account,
- denial of service through unrealistic request volumes,
- vulnerabilities in third-party services you configured yourself (model gateway, MCP servers),
- missing hardening headers with no demonstrated impact.

## Deployment hardening checklist

- Set a unique `SECRET_KEY` and `JWT_SIGNING_KEY` per environment; never reuse the sample values.
- Keep `DEBUG=false` and `ALLOW_REGISTER=false` in production.
- Serve over HTTPS and set `AUTH_TRUSTED_PROXIES` only for proxies you control.
- Restrict database access to the application host; do not expose PostgreSQL or Qdrant publicly.
- Rotate `MCP_SECRET_KEY` only with a planned re-entry of stored MCP credentials.
- Review the audit log for `agent_import_*`, `skill_update` and permission changes.
