# Contributing to SOLVIO Core

SOLVIO Core is a security-sensitive personal-agent runtime. A contribution is acceptable only if it works **and** preserves authority, trust, credential and execution boundaries.

## Before opening a pull request

1. Read `README.md` (the principles are the contract) and the module docstrings of the
   area you touch; they state the boundary each module defends and why.
2. Search existing issues and pull requests.
3. For architectural changes, open a design issue first.
4. Keep the change narrowly scoped.
5. Add or update tests.
6. Update documentation whenever behavior or a contract changes.
7. Run the canonical test entrypoint before requesting review:

```bash
python3 scripts/run_tests.py
```

## Security rules

Do not commit or paste:

- credentials, tokens or cookies
- real `.env` files
- private keys or device-attestation material
- personal memory databases
- private voice/audio captures
- production logs containing user conversations
- installation-specific secrets or private deployment state

Treat model output, web pages, documents, messages, logs and executor output as **untrusted data**. They do not gain authority by containing an instruction.

Test fixtures must be synthetic: reserved documentation addresses and domains (`example.*`, `*.test`, `*.invalid`, RFC 1918 examples that belong to no real installation), the Ofcom drama range for phone numbers, and credential-shaped values that carry a visible marker (`CANARY`, `not-a-real`, repeated characters). A realistic-looking key, a real mailbox or a real network address in a fixture is treated as a leak, even if it is fake.

A pull request must not create a path by which a model or external content can:

- approve its own action
- reduce its own risk level
- mint user authority
- alter its own trust context
- bypass an approval gate
- obtain reusable provider credentials from an untrusted executor

## Pull-request expectations

Explain:

- what changed and why
- which trust/security boundary is affected
- tests that prove intended behavior
- any new credential, network, filesystem or persistence surface
- what was deliberately not changed

## AI-assisted contributions

AI-assisted contributions are welcome. The contributor remains responsible for correctness, licensing and provenance. If a non-trivial implementation was adapted from another project, identify the upstream project and license in the pull request.
