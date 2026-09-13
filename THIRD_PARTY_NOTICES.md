# Third-party notices

SOLVIO Core is licensed under the Apache License, Version 2.0 (see `LICENSE` and
`NOTICE`). This file lists the third-party software SOLVIO Core **depends on**. It is
provided for attribution and transparency.

**Scope.** SOLVIO Core is published as source. It does not vendor, bundle or
redistribute any of the packages below: they are declared as dependencies in
`pyproject.toml` / `uv.lock` and are obtained by the user from their own distribution
channel (PyPI) under their own licenses. Nothing in this file claims rights over
third-party code, and nothing below changes the licenses of those projects. If you
build and redistribute a packaged form of SOLVIO Core (wheels, container images,
installers) that embeds these packages, you take on their notice and attribution
obligations yourself — including those of the native libraries some wheels bundle.

The license information below was read from the distributions' own metadata
(`License-Expression`, `License-File`, classifiers) at the versions pinned in
`uv.lock` on 2026-09-13 and cross-checked against the PyPI metadata of the same
packages.

## Runtime dependencies (direct)

| Package | Version (lock) | License | Project |
|---|---|---|---|
| aiohttp | 3.14.3 | Apache-2.0 AND MIT (the wheel vendors `llhttp` under MIT) | https://github.com/aio-libs/aiohttp |
| websockets | 17.0.1 | BSD-3-Clause | https://github.com/python-websockets/websockets |
| pydantic | 2.13.4 | MIT | https://github.com/pydantic/pydantic |
| pydantic-settings | 2.15.0 | MIT | https://github.com/pydantic/pydantic-settings |
| structlog | 26.1.0 | MIT OR Apache-2.0 | https://github.com/hynek/structlog |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause (wheels bundle OpenSSL, Apache-2.0) | https://github.com/pyca/cryptography |
| cbor2 | 6.1.4 | MIT | https://github.com/agronholm/cbor2 |
| pyrage | 1.4.0 | MIT (see below) | https://github.com/woodruffw/pyrage |

### Notices carried by these projects

Where a dependency ships a `NOTICE` file, its text is reproduced here so that the
attribution travels with any packaged redistribution:

- **structlog** — `NOTICE`: "structlog — Copyright 2013 Hynek Schlawack and the
  structlog contributors".
- **aiohttp** — Apache-2.0 for aiohttp itself; the vendored `llhttp` parser is MIT
  ("Copyright © 2018 Fedor Indutny", see `licenses/vendor/llhttp` in the distribution).

### pyrage

pyrage is a Python binding (MIT, Copyright (c) 2022 William Woodruff) around the
Rust `age` implementation. Its **binary wheel** bundles compiled Rust crates; the
distribution ships a CycloneDX SBOM (`pyrage-1.4.0.dist-info/sboms/`) listing 165
components, essentially all under `MIT OR Apache-2.0`, MIT, BSD-3-Clause or
Unlicense (the `age`/`age-core` crates: MIT OR Apache-2.0). SOLVIO Core's source
release neither vendors pyrage nor its wheel; anyone redistributing a build that
embeds the wheel must honour those crate licenses (all permissive, MIT/Apache-2.0
dual-licensed where applicable).

## Optional dependencies

| Package | Version (lock) | Extra | License | Project |
|---|---|---|---|---|
| sentence-transformers | 6.0.0 | `embeddings` | Apache-2.0 | https://www.SBERT.net |
| pytest | 9.1.1 | `dev` | MIT | https://docs.pytest.org |

- **sentence-transformers** — `NOTICE.txt`: "Sentence Transformers — Copyright
  2019-2025 Ubiquitous Knowledge Processing (UKP) Lab, Technische Universität
  Darmstadt; Copyright 2025-present Hugging Face, Inc." The `embeddings` extra pulls
  torch, transformers, huggingface-hub, scikit-learn, scipy and numpy (Apache-2.0 /
  BSD-3-Clause families). Model weights downloaded at run time are **not** part of
  SOLVIO Core and carry their own licenses.

## Build dependency

| Package | License | Project |
|---|---|---|
| hatchling | MIT | https://hatch.pypa.io |

hatchling is used only to build the package; it is not a runtime dependency.

## Transitive dependencies

The complete pinned dependency set is in `uv.lock`. Every transitive distribution in
the locked set declares a permissive license (MIT, MIT-0, BSD-2/3-Clause, 0BSD, ISC,
Apache-2.0 incl. the LLVM exception, PSF-2.0, Zlib, CC0-1.0, CNRI-Python) or MPL-2.0
(certifi, tqdm — file-level copyleft, unmodified use). No GPL-family license is
present.

## Embedded public key material

`src/solvio/security/mobile_approval/app_attest.py` embeds the **Apple App Attestation
Root CA** certificate (public trust anchor, SHA-256 fingerprint
`1CB9823BA28BA6AD2D33A006941DE2AE4F513EF1D4E831B9F7E0FA7B6242C932`), byte-identical to
the certificate Apple publishes at https://www.apple.com/certificateauthority/ and
references in its App Attest server-validation documentation. It is a certificate, not
third-party source code; it is included solely to verify attestations against Apple's
published trust anchor.

## External systems (not distributed)

SOLVIO Core integrates with external services and programs over their APIs and
processes. None of their code is included in this repository:

- **Hermes** (`hermes-agent` by Nous Research, MIT,
  https://github.com/NousResearch/hermes-agent) — an external agent runtime that
  SOLVIO drives as a sandboxed peer over HTTP; the integration layer in
  `src/solvio/deep/` is SOLVIO code.
- Provider APIs (OpenAI, Anthropic, ElevenLabs, Google, AWS S3) and Home Assistant are
  reached over their public interfaces under their own terms.
