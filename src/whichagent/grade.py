"""Grade a finished run: capture the diff, measure it, reset the tests, run the real ones."""
import os
import re
import shlex

from . import config as c, git as g

RUNNABLE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|[^/]*\.(test|spec)\.[cm]?[jt]sx?|__tests__/[^/]*\.[cm]?[jt]sx?)$")


def runnable(paths):
    """Test files a runner can take as arguments: not fixtures, conftest or helpers."""
    return [p for p in paths if RUNNABLE.search(p)]


def _quote(s):
    return shlex.quote(s) if os.name != "nt" else f'"{s}"' if " " in s else s


def test(cfg, ws, files, log):
    """Run the test command on files ([] = the full suite). Returns (exit code or None, seconds)."""
    cmd = cfg["repo"]["test"].replace("{files}", " ".join(map(_quote, files)))
    return g.proc(cmd, ws, c.secs(cfg["repo"]["test_timeout"]), log, shell=True)


def grade(cfg, task, ws, start, run_dir):
    """Measure the agent's changes against the start tree, then test with the real tests in place."""
    root = cfg["root"]
    changed = g.changes(ws, start)
    (run_dir / "agent.diff").write_text(g.diff(ws, start), "utf-8")
    tests = {p: f for p, f in changed.items() if c.is_test(cfg, p)}
    r = {"files_changed": len(changed),
         "extra_files": sum(p not in tests and p not in task["source_files"] for p in changed),
         "wrote_tests": any(f[1] > 0 and not c.match(p, c.TEST_CONFIG) for p, f in tests.items()),
         # removing lines from an existing test, or touching test config, counts; adding tests doesn't
         "tampered": any(f[0] != "A" and f[2] > 0 or f[0] == "D" or c.match(p, c.TEST_CONFIG)
                         for p, f in tests.items()),
         "flagged": [p for p in changed if c.match(p, c.FLAGGED)]}
    g.put(root, task["base"], list(tests), ws)
    g.put(root, task["fix"], task["test_files"], ws)
    r["tests_pass"] = test(cfg, ws, runnable(task["test_files"]), run_dir / "tests.log")[0] == 0
    r["regression"] = None  # only checked when the task's tests pass and the suite was green at the fix
    if r["tests_pass"] and task["suite_green"] and cfg["run"]["regression"]:
        r["regression"] = test(cfg, ws, [], run_dir / "suite.log")[0] != 0
    return r
