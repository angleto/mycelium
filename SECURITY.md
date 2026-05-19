# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not via public issues or
pull requests.

- Use GitHub's **private vulnerability reporting**: repository →
  **Security** → *Report a vulnerability*. This opens a private
  security advisory visible only to the maintainers — no public
  disclosure and no need to share a personal contact.

Include enough to reproduce: affected component (API, web, MCP,
worker, deploy), version/commit, impact, and steps or a proof of
concept. You will get an acknowledgement within a reasonable time and
a fix or mitigation plan once the report is triaged.

Please do not run automated scanners against any hosted instance, and
do not access, modify or exfiltrate data that is not yours. Good-faith
research that respects these limits is welcome.

## Supported versions

This is an evolving project. Security fixes target the **default
branch** (`main`); there is no long-term-support branch yet. If you
self-host, track `main` (or a tagged release once releases are cut).

## Scope and design notes

Flow is multi-tenant with PostgreSQL **row-level security** as the
isolation boundary and a sudo-clamped effective-role model for
privileged operations. Especially valuable reports: tenant isolation
or RLS bypass, authentication/authorization or effective-role
escalation, secret handling (the Fernet envelope for opaque secrets),
the MCP control surface, and the agent execution runtime
(tool-allowlist / budget guardrails).

Deployment and CI/CD configuration are kept in a separate private
repository; this repository contains application code only and ships
no production secrets. Test/dev fixtures use obvious dummy values, not
real credentials.

## Disclosure

Coordinated disclosure: we will agree on a timeline before any public
write-up, and credit reporters who want it.
