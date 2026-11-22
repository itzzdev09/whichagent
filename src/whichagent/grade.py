"""Grade a finished run: capture the diff, measure it, reset the tests, run the real ones."""
import os
import re
import shlex

from . import config as c, git as g, testresults

RUNNABLE = re.compile(
    r"(^|/)(test_[^/]*\.py|[^/]*_test\.py"                                 # pytest, unittest
    r"|[^/]*\.(test|spec)\.[cm]?[jt]sx?|__tests__/[^/]*\.[cm]?[jt]sx?"     # vitest, jest, node --test
    r"|[^/]*_test\.go|tests/[^/]*\.rs"                                     # go test, cargo integration tests
    r"|[^/]*Tests?\.(java|kt)|Test[^/]*\.(java|kt))$")                     # JUnit via maven or gradle


def runnable(paths):
    """Test files a runner can take as arguments: not fixtures, conftest or helpers."""
    return [p for p in paths if RUNNABLE.search(p)]


def _quote(s):
    return shlex.quote(s) if os.name != "nt" else f'"{s}"' if re.search(r"[\s&|<>^()]", s) else s


def command(template, files, report=""):
    """template with {files}, {dirs} (./dir per file, for go test), {names} (file stems, for cargo --test and
    JUnit classes) and {report} filled in. {names:,} joins with "," instead of " "."""
    lists = {"files": files,
             "dirs": list(dict.fromkeys("./" + p.rsplit("/", 1)[0] if "/" in p else "." for p in files)),
             "names": list(dict.fromkeys(p.rsplit("/", 1)[-1].split(".")[0] for p in files))}
    cmd = c.PLACEHOLDER.sub(lambda m: (" " if m[2] is None else m[2]).join(map(_quote, lists[m[1]])), template)
    return cmd.replace("{report}", _quote(report))


def test(cfg, box, ws, files, log, suite=False):
    """Run the task's test files, or the whole suite. Returns (exit code or None, seconds, {test: passed} or None)."""
    rp = cfg["repo"]
    template = rp["suite"] or c.PLACEHOLDER.sub("", rp["test"]) if suite else rp["test"]
    report = ws.parent / f"{log.stem}.report"  # outside the workspace, inside what docker mode mounts
    fixed = ws / rp["report"] if rp["report"] else None
    for stale in (report, fixed):
        if stale and stale.is_dir():
            g.rmtree(stale)
        elif stale:
            stale.unlink(missing_ok=True)
    rc, secs = box.shell(command(template, files, box.path(report)), c.secs(rp["test_timeout"]), log)
    found = report if report.exists() else fixed
    if found and not found.resolve().is_relative_to(ws.parent.resolve()):
        found = None  # a symlink the agent's code planted, to make this host process read a file of yours
    return rc, secs, testresults.parse(found, log.read_text("utf-8", "replace"), box.roots)


def grade(cfg, task, ws, box, start, run_dir, scope=()):
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
         "flagged": [p for p in changed if c.match(p, c.FLAGGED)],
         "out_of_scope": sorted(p for p in changed if scope and not c.match(p, scope))}
    g.put(root, task["base"], list(tests), ws)
    g.put(root, task["fix"], task["test_files"], ws)
    rc, _, results = test(cfg, box, ws, runnable(task["test_files"]), run_dir / "tests.log")
    f2p, p2p = task.get("fail_to_pass"), task.get("pass_to_pass") or []
    if f2p and results is not None:  # per test, as SWE-bench does: the fixed tests pass, the others still do
        r["f2p"] = f"{sum(results.get(t) is True for t in f2p)}/{len(f2p)}"
        r["p2p"] = f"{sum(results.get(t) is True for t in p2p)}/{len(p2p)}"
        r["tests_pass"] = all(results.get(t) is True for t in f2p + p2p)
    else:
        r["tests_pass"] = rc == 0
    r["regression"] = None  # checked only when the task's tests pass
    failing = task.get("suite_failing")
    if r["tests_pass"] and cfg["run"]["regression"] and (failing is not None or task["suite_green"]):
        rc, _, results = test(cfg, box, ws, [], run_dir / "suite.log", suite=True)
        if failing is not None and results is not None:  # anything that passed at the fix and fails now
            broke = {t for t, ok in results.items() if not ok and t not in failing}
            if broke:  # a flaky test shouldn't fail the agent: it counts only if it fails twice in a row
                again = test(cfg, box, ws, [], run_dir / "suite-again.log", suite=True)[2] or {}
                broke = {t for t in broke if again.get(t) is not True}
            r["regression"], r["broke"] = bool(broke), sorted(broke)[:20]
        elif task["suite_green"]:
            r["regression"] = rc != 0
    return r
