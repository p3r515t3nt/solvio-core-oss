# Security Policy

SOLVIO Core is security-sensitive software. Security reports that could expose credentials, personal data, approval bypasses, sandbox escapes or unauthorized actions must not be filed as public issues.

## Reporting

Use GitHub Private Vulnerability Reporting when it is enabled for this repository. Until then, request a private reporting channel from the maintainer without publishing exploit details.

Use synthetic secrets and test data in reports.

## High-priority security boundaries

Reports are especially relevant when they involve:

- approval or authority bypass
- prompt/content injection becoming authority
- credential exfiltration or unsafe provider-secret reuse
- cross-capability privilege escalation
- sandbox/executor escape
- replay of revoked or expired approvals
- memory disclosure
- browser access to local/private infrastructure
- unsafe file access or SSRF/redirect bypass
- incorrect trust/provenance labeling
- sensitive conversation/credential logging

## Supported versions

While the project is pre-1.0 and interfaces are evolving, security fixes target the maintained default branch and latest tagged release, if one exists.

## Disclosure

Please allow reasonable time for reproduction, remediation and coordinated disclosure before publishing vulnerability details.
