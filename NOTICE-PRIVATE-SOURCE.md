# Source provenance note

This public snapshot is exported from a separate private operational repository.
The private repository's Git history is intentionally not included because it
contains installation-specific operational records that are not required to use
or contribute to the reusable software.

A small number of installation-specific defaults were rewritten for the public
distribution (configurable paths, the Apple Team ID of the companion app, the
owner name used on the phone). Test suites that audit the private knowledge base
or the export tooling are not part of the public tree. The exporter's audit
report — kept with the private repository, not published — lists every rewrite,
every excluded suite and every scanner finding that was accepted as synthetic
test data, with the rule that accepted it.

Omitting private operational history does not waive third-party attribution or
license obligations: SOLVIO Core is licensed under the Apache License, Version 2.0
(`LICENSE`, `NOTICE`), and the licenses of its dependencies are recorded in
`THIRD_PARTY_NOTICES.md`.
