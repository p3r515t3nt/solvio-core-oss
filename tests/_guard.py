"""Assertion integrity guard — imported for its side effect.

P1A.4/C2. Most suites in this repo use bare `assert`, which `python -O` / `PYTHONOPTIMIZE`
strips. That is not a theoretical concern: with a security guard deleted from production,
`python -O tests/test_f5_1_atomic_begin.py` printed `=== 6/6 bestanden ===` and exited 0,
where normal Python printed 5/6. A security suite that cannot fail is worse than no suite —
it manufactures confidence.

Two defences, because neither alone is sufficient:

1. This module refuses to load under optimized Python. It is imported by the shared test
   helper, so every suite that uses it inherits the guard however it is invoked.
2. `require()` below is a real function call. It survives `-O`, so tests written with it
   keep their teeth even if this guard were somehow bypassed.

The guard deliberately does NOT use `assert` — that would be self-defeating.
"""
import os
import sys
import tempfile

# KEIN TEST OEFFNET JE DEN PRODUKTIVEN KONTAKTSPEICHER.
#
# `BindingStore()` ohne Pfad faellt auf `~/.solvio/contacts.sqlite3` zurueck —
# die Datei, aus der der laufende Core liest, wen „mich" und „mein Sohn"
# meinen. Eine Zusicherung tat genau das (`CommunicationCapabilities(gmail=None)`
# ohne Speicher), jahrelang folgenlos, weil das Oeffnen nur `CREATE TABLE IF
# NOT EXISTS` ausfuehrte. Mit der additiven Schema-Nachziehung von Contact
# Binding Authority Hardening V1 wurde daraus beim ersten Suitenlauf eine
# Migration des produktiven Bestands — gemessen am 2026-09-05 an der WAL-Datei,
# rollback-sicher und ohne veraenderte Zeile, aber ein Schreibzugriff eines
# Tests auf Production. Dieser Guard liegt hier, weil ihn jede Suite laedt:
# der Standardpfad zeigt unter Tests auf ein leeres Verzeichnis, und nur eine
# ausdruecklich gesetzte Umgebung kann das aendern.
if "SOLVIO_CONTACTS_DB" not in os.environ:
    os.environ["SOLVIO_CONTACTS_DB"] = os.path.join(
        tempfile.mkdtemp(prefix="solvio-test-contacts-"), "contacts.sqlite3")

_MESSAGE = (
    "Security regression must not run under optimized Python.\n"
    "  `assert` statements are stripped by -O / PYTHONOPTIMIZE, so these suites would\n"
    "  report success without checking anything.\n"
    f"  sys.flags.optimize = {sys.flags.optimize}\n"
    "  Re-run without -O, e.g.:  python3 scripts/run_tests.py"
)

if sys.flags.optimize != 0:  # pragma: no cover - the whole point is that it exits
    print(_MESSAGE, file=sys.stderr)
    raise SystemExit(2)


def enforce_assertions() -> None:
    """Explicit bootstrap every standalone test entry point calls.

    P1A.5/H2: the previous guard only fired if a suite happened to import
    `mobile_attest_helper`, which covered 11 of 25 suites. Optimize-safety must not be an
    accident of an unrelated import — with the S1 broker's digest binding deleted,
    `python -O tests/test_approval.py` printed 17/17 where normal Python printed 16/17.
    Importing this module already exits; this function makes the dependency explicit and
    survives an import reordering.
    """
    if sys.flags.optimize != 0:  # pragma: no cover
        print(_MESSAGE, file=sys.stderr)
        raise SystemExit(2)


class RequirementFailed(AssertionError):
    """A checked security requirement did not hold."""


def require(condition, message: str = "") -> None:
    """Assert that survives `-O`. Use this in security-critical tests."""
    if not condition:
        raise RequirementFailed(message or "required condition was false")


def require_equal(actual, expected, message: str = "") -> None:
    if actual != expected:
        raise RequirementFailed(
            f"{message or 'values differ'}: expected {expected!r}, got {actual!r}")


def require_raises(exc_types, fn, *args, message: str = "", **kwargs):
    """Run `fn` and require it to raise. Returns the exception for further inspection."""
    try:
        result = fn(*args, **kwargs)
    except exc_types as caught:
        return caught
    raise RequirementFailed(
        f"{message or 'expected an exception'}: {fn!r} returned {result!r} instead")


def require_private_material(*relative_paths: str) -> None:
    """Skip when the private repository material a test reads is not in this tree.

    The public snapshot ships `src/`, `tests/`, `scripts/` and `config/` — not the
    knowledge base (`docs/`, `ROADMAP.md`, `PROJECT.md`) and not the deployment state
    (`deploy/`). A test that reads those audits the PRIVATE repository: outside it the
    precondition is absent, and the test is SKIPPED with this reason, visible in the
    runner's SKIP column — never silently green. Inside the private repository every
    named path exists and the skip never triggers, so the private gate is unchanged.
    """
    import unittest
    repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    missing = [p for p in relative_paths if not os.path.exists(os.path.join(repo, p))]
    if missing:
        raise unittest.SkipTest(
            "private repository material absent: " + ", ".join(missing))


def require_tool(*candidates: str, why: str = "") -> str:
    """Skip unless one of the tools exists; return the path of the first that does.

    A candidate is an absolute path or a program name looked up on PATH. The suites
    measure real mechanisms — macOS `sandbox-exec`, the keychain's `security`, `lsof`,
    the Codex and Claude CLIs — and outside the machine that has them the precondition
    is absent, not the guarantee. The skip names the missing tool; on the canonical
    macOS gate every candidate exists and nothing is skipped.
    """
    import shutil
    import unittest
    for candidate in candidates:
        if os.path.isabs(candidate):
            if os.path.exists(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    raise unittest.SkipTest(
        f"tool not available on this machine: {', '.join(candidates)}"
        + (f" — {why}" if why else ""))


def require_darwin(why: str = "") -> None:
    """Skip on platforms other than macOS for mechanisms that only exist there."""
    import unittest
    if sys.platform != "darwin":
        raise unittest.SkipTest(f"needs macOS ({sys.platform})" + (f" — {why}" if why else ""))
