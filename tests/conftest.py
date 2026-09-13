"""pytest integration (optional — `scripts/run_tests.py` is the canonical path).

Two things pytest needs here, both consequences of how these suites are written:
  * the security tests are named `t_*` (see pyproject `python_functions`), and
  * most of them are `async def` with no asyncio plugin installed.
The hook below runs coroutine tests directly, so no extra dependency is required.

P1A.4/C2: the optimize guard is repeated here because pytest is a second entry point and
must not be a way around it. It is not an `assert` — that would be self-defeating.
"""
import asyncio
import os
import shutil
import sys
import tempfile

import pytest

if sys.flags.optimize != 0:  # pragma: no cover
    raise SystemExit(
        "Security regression must not run under optimized Python: -O strips `assert`, "
        "so these suites would report success without checking anything.")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    fn = pyfuncitem.obj
    if asyncio.iscoroutinefunction(fn):
        kwargs = {n: pyfuncitem.funcargs[n] for n in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(fn(**kwargs))
        return True
    return None


# The canonical runner gives every suite its own TMPDIR and removes it after a pass (see
# `_suite_tmpdir` in scripts/run_tests.py). pytest is the second entry point, so it gets the
# same hygiene at session scope: the directory is created before collection (module-level
# sandboxes are allocated at import time), and removed only when the whole session passed.
_SESSION_TMP: dict = {}


def pytest_configure(config):
    base = tempfile.gettempdir()
    tmp = tempfile.mkdtemp(prefix="solvio-pytest-", dir=base)
    _SESSION_TMP.update(base=base, tmp=tmp, previous=os.environ.get("TMPDIR"))
    os.environ["TMPDIR"] = tmp
    tempfile.tempdir = tmp


def pytest_sessionfinish(session, exitstatus):
    info = _SESSION_TMP
    if not info:
        return
    tempfile.tempdir = None
    if info["previous"] is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = info["previous"]
    keep = exitstatus != 0 or os.environ.get("SOLVIO_KEEP_TEST_TMP") == "1"
    tmp = info["tmp"]
    ours = (os.path.dirname(tmp) == info["base"] and os.path.basename(tmp).startswith("solvio-pytest-")
            and os.path.isdir(tmp) and not os.path.islink(tmp))
    if keep or not ours:
        print(f"\ntemp artifacts kept for diagnosis: {tmp}")
        return
    shutil.rmtree(tmp, ignore_errors=True)
