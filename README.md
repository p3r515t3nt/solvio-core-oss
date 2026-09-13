# SOLVIO Core

**A security-first, provider-independent runtime for personal AI agents.**

SOLVIO Core is the central service of a personal AI assistant. It owns the conversation,
the memory, the authority to act, the capabilities the assistant can use and the audit
trail of what it did. Language models, agent runtimes, browsers, smart-home systems and
telephony providers are dependencies behind generic contracts — the runtime owns its
dependencies, not the other way round.

This repository is a reviewed public snapshot of a runtime that is in daily private use.
It is published so that the *architecture* — in particular the way authority is kept
apart from information — can be read, criticised and reused. Interfaces are still
evolving and there is no stability promise yet.

## Principles

- **Information is not authority.** Web pages, e-mails, documents, portal content, phone
  partners and the output of any model or executor may *inform* the system. None of them
  can approve anything, lower a risk level, change a trust context or mint permissions.
- **Models are not the authority.** A model may *request* an action; it can never approve
  its own request. A denied approval is final — the runtime does not look for a way
  around it.
- **External content is untrusted.** Everything that arrives from outside the Core is
  labelled with its provenance (`user_direct`, `untrusted_executor`, `untrusted_content`,
  …) and that label travels with the data.
- **Capabilities, not commands.** Actions are explicit capabilities with a contract: risk
  class, idempotency semantics, bound parameters and a result truth that never claims more
  than the evidence supports (a started call is not a delivered message).
- **Explicit approval boundaries.** Externally effective actions need bound parameters,
  bound authority and — depending on origin and action class — a user decision proven by
  the companion app (device attestation plus biometrics). Approved parameters cannot be
  changed silently afterwards.
- **Providers and executors are replaceable.** Reasoning providers, the deep executor,
  telephony, e-mail and smart-home integrations sit behind SOLVIO-owned contracts.
  Provider credentials never reach an untrusted executor; a short-lived broker token
  does.
- **Local-first and privacy-aware.** Conversation, memory, approvals and the secret vault
  are local. Backups are encrypted before they leave the machine. Personal data is
  minimised and redacted by default, including on diagnostic paths.
- **Fail closed.** Missing configuration, an unverifiable attestation, an unknown policy
  value or an unclear result state disables the action instead of guessing.

## Repository layout

    src/solvio/
      realtime/        voice runtime, satellite protocol, session ownership
      conversation/    conversation ownership and history
      memory/          long-term memory, embeddings, provenance, adaptive learning
      security/        approval and authority — the frozen security core
      capabilities/    capability registry, contracts, approval gateway
      contracts/       trust and risk semantics
      cognition/       task routing between conversation, research and agents
      agent_runtime/   bounded agent runs with isolated workspaces
      provider_broker/ provider credentials stay in the Core; executors get leases
      secret_vault/    secret references instead of secret values
      deep/            deep executor (sandboxed) integration
      browser/ portal/ public web access and authenticated portals
      research/        quick and deep research with source provenance
      communication/ telephony/ contact binding, calls and messages
      payment/         payment authority without holding payment instruments
      storage/         local backup, encrypted offsite backup, inventory
      remote_access/   remote reachability without granting authority
      control_center/  state view for the companion app
      proactive/ background/  schedules, inbox, unattended runs
      doctor/          diagnosis and allowlisted repair
      integrations/    Home Assistant, Google
    tests/             behaviour suites (see "Tests")
    scripts/           the canonical test runner and its helpers

## Requirements

- Python 3.13
- [`uv`](https://docs.astral.sh/uv/)
- macOS is the primary platform of the private installation (launchd, Keychain, App
  Attest). Large parts of the runtime are platform-neutral, but macOS-specific paths are
  not abstracted everywhere yet.

## Development

```bash
uv sync --frozen --all-extras   # --all-extras: the local embedding model the memory suites use
cp .env.example .env            # placeholders; fill in your own values — the backup
                                # inventory expects this file to exist, tests included
python3 scripts/run_tests.py    # the canonical test path
```

`scripts/run_tests.py` is the **only** authoritative test path. It refuses to run under
optimized Python (which strips `assert`), discovers every `tests/**/test_*.py`, compares
the executed test identities against a tracked baseline and reports
collected / executed / passed / failed / skipped separately — never "N green" for tests
that never ran. Plain `pytest` works for local iteration but does not carry that
contract.

Filter suites by name:

```bash
python3 scripts/run_tests.py memory approval
```

## Configuration

`.env.example` documents every setting. Secrets belong in `.env` (never committed) or in
the secret vault; the configuration layer only ever reports whether a secret is set, not
its value. Installation-specific values (Apple Team ID of the companion app, owner name,
an optional checkout override) are environment variables in the public distribution; the
Core repository defaults to the checkout the code runs from.

## External systems and integrations

SOLVIO Core drives a number of external systems over their public interfaces. None of
their code is part of this repository:

- **Hermes** — the deep executor is [`hermes-agent`](https://github.com/NousResearch/hermes-agent)
  by Nous Research (MIT), run as a separate, sandboxed process that SOLVIO talks to over
  HTTP through its own provider broker. The integration layer (`src/solvio/deep/`,
  `src/solvio/bots/`) is SOLVIO code; SOLVIO contains no Hermes source.
- Reasoning and voice providers (OpenAI, Anthropic, ElevenLabs), Google Calendar/Gmail,
  Home Assistant and S3-compatible object storage are used through their APIs behind
  SOLVIO-owned contracts.

Third-party Python dependencies and their licenses are listed in
`THIRD_PARTY_NOTICES.md`.

## Status

This snapshot is exported from a private operational repository with a fresh history;
see `NOTICE-PRIVATE-SOURCE.md`. Operational runbooks, deployment state, architecture
decision records and the release history of the private installation are intentionally
not part of the public tree.

## Security

Read `SECURITY.md` before reporting a vulnerability. Never post real credentials, private
conversation logs, memory databases or installation-specific data.

## Contributing

See `CONTRIBUTING.md`. Contributions must preserve the authority, trust, credential and
execution boundaries described above.

## License

Copyright © 2026 Gregor Brawanski

SOLVIO Core is licensed under the Apache License, Version 2.0 — see `LICENSE` and
`NOTICE`. Third-party dependencies are distributed under their own licenses, listed in
`THIRD_PARTY_NOTICES.md`.

**Trademarks.** The SOLVIO name and logo are not licensed under the Apache License 2.0.
Using, modifying or redistributing the source code does not grant any right to use the
SOLVIO name or logo to identify your own products or services (see section 6 of the
license).
