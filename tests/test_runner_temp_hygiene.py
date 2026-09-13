"""The canonical runner owns every suite's temporary directory.

Measured on the production Mac on 2026-09-13: 215 144 leftover `solvio-*` directories held
7.9 GB of the internal disk, and one full gate added about a thousand more. Cause: the
suites allocate `tempfile.mkdtemp()` sandboxes — at module level, in per-test helpers
(`fresh()`, `_ledger()`), and in subprocesses — and the plain-function harness has no
teardown that could remove them. The fix lives where the lifecycle is known: the runner
gives each suite its own directory under the real temp location, points TMPDIR at it,
removes it after a clean pass and keeps it — by name — after anything else.

Proven here by behaviour and counter-example, against the real runner:

* a passing suite writes its sandboxes into the runner-owned directory and leaves nothing
  behind — including sandboxes created by a child process the suite spawned;
* a failing suite keeps its directory and the runner names the path;
* `SOLVIO_KEEP_TEST_TMP=1` keeps the directory of a passing suite too;
* the runner never removes a directory it did not create (a foreign path is left alone).
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

import _inventory  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _runner():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    return importlib.import_module("run_tests")


def _harness_suite(tmp: str, body: str, name: str = "test_probe_tmp.py") -> str:
    path = os.path.join(os.path.realpath(tmp), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"import os, sys, tempfile\nsys.path.insert(0, {TESTS_DIR!r})\n"
                 "from _guard import enforce_assertions\nenforce_assertions()\n" + body +
                 '\nif __name__ == "__main__":\n'
                 "    from _harness import run_module\n"
                 "    raise SystemExit(run_module(globals(), __name__))\n")
    return path


def _run(path: str):
    runner = _runner()
    return runner.run_one(Path(path), _inventory.suite_expected(path),
                          invalid_defs=_inventory.suite_invalid(path))


#: A probe suite that behaves like the real ones: a module-level sandbox, a per-test
#: sandbox, and a child process that creates its own. It reports where they landed.
_PROBE_BODY = '''
import json, subprocess
_SANDBOX = tempfile.mkdtemp(prefix="solvio-probe-module-")
REPORT = os.environ["PROBE_REPORT"]

def test_sandboxes_land_in_the_runner_owned_directory():
    own = tempfile.mkdtemp(prefix="solvio-probe-test-")
    child = subprocess.run([sys.executable, "-c",
                            "import tempfile; print(tempfile.mkdtemp(prefix='solvio-probe-child-'))"],
                           capture_output=True, text=True, check=True).stdout.strip()
    with open(os.path.join(own, "artifact.txt"), "w") as fh:
        fh.write("x")
    with open(REPORT, "w") as fh:
        json.dump({"module": _SANDBOX, "test": own, "child": child,
                   "tmpdir": tempfile.gettempdir()}, fh)
'''


def _probe(outcome_body: str = ""):
    """Run the probe suite; return (result, report dict)."""
    import json
    host = tempfile.mkdtemp(prefix="solvio-tmp-hygiene-host-")
    report = os.path.join(host, "report.json")
    os.environ["PROBE_REPORT"] = report
    try:
        path = _harness_suite(host, _PROBE_BODY + outcome_body)
        res = _run(path)
    finally:
        os.environ.pop("PROBE_REPORT", None)
    data = json.load(open(report, encoding="utf-8")) if os.path.exists(report) else {}
    return res, data


def t_a_passing_suite_leaves_nothing_behind():
    res, data = _probe()
    require_equal(res.failed, 0, f"the probe should pass: {res.failures}")
    require(res.tmp_dir, "the runner allocated a directory for the suite")
    for key in ("module", "test", "child"):
        require(data[key].startswith(res.tmp_dir + os.sep),
                f"{key} sandbox {data[key]} is outside the runner-owned {res.tmp_dir}")
    require_equal(data["tmpdir"], res.tmp_dir, "the suite's tempfile followed TMPDIR")
    require(not os.path.exists(res.tmp_dir), "the directory survived a clean pass")
    require(not res.tmp_kept, "a passing suite must not report kept artifacts")


def t_a_failing_suite_keeps_its_artifacts_and_names_them():
    res, data = _probe("\ndef test_that_fails():\n    raise AssertionError('boom')\n")
    require(res.failed > 0, "the probe should fail")
    require(os.path.isdir(res.tmp_dir), "a failed suite's directory was removed")
    require(os.path.isfile(os.path.join(data["test"], "artifact.txt")),
            "the artifact a diagnosis needs is gone")
    require(res.tmp_kept, "the result does not say the directory was kept")
    require(any(res.tmp_dir in line and "kept for diagnosis" in line for line in res.failures),
            f"the kept path is not named in the output: {res.failures}")
    # the test leaves no trace of its own: the probe's directory is disposable
    import shutil
    shutil.rmtree(res.tmp_dir, ignore_errors=True)


def t_the_keep_switch_keeps_a_passing_suite_too():
    os.environ[_runner().TMP_KEEP_ENV] = "1"
    try:
        res, data = _probe()
    finally:
        os.environ.pop(_runner().TMP_KEEP_ENV, None)
    require_equal(res.failed, 0, f"the probe should pass: {res.failures}")
    require(os.path.isdir(res.tmp_dir), "the keep switch was ignored")
    require(res.tmp_kept, "the result does not say the directory was kept")
    import shutil
    shutil.rmtree(res.tmp_dir, ignore_errors=True)


def t_the_runner_only_removes_directories_it_created():
    """Counter-example: a result pointing at a foreign directory must leave it alone."""
    runner = _runner()
    foreign = tempfile.mkdtemp(prefix="not-a-suite-dir-")
    try:
        with open(os.path.join(foreign, "keep.txt"), "w") as fh:
            fh.write("do not delete")
        res = runner.Result(Path("probe.py"))
        res.tmp_dir = foreign
        runner._dispose_suite_tmpdir(res, tempfile.gettempdir())
        require(os.path.isfile(os.path.join(foreign, "keep.txt")),
                "the runner removed a directory it did not create")
        require(res.tmp_kept and any("not owned" in line for line in res.failures),
                f"a foreign directory must be reported, not removed: {res.failures}")
    finally:
        import shutil
        shutil.rmtree(foreign, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
