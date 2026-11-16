"""Find fix commits that came with tests; keep the ones whose tests fail before the fix and pass after."""
import concurrent.futures as cf
import itertools
import json
import re
import shutil
import subprocess

from . import config as c, git as g, grade, sandbox as sb, store, workspace

LOAD_ERROR = re.compile(r"ImportError|ModuleNotFoundError|errors? during collection|Cannot find module"
                        r"|does not provide an export named|undefined: |cannot find (?:symbol|value|function)")
FIXES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)", re.I)


def candidates(cfg, gh):
    """Cheap filter over history: small source change plus runnable tests, no dependency changes."""
    r, m, out = cfg["repo"], cfg["mine"], []
    for cm in g.commits(cfg["root"], m["since"]):
        fs = cm["files"]
        tests = {p for p in fs if c.is_test(cfg, p)}
        srcs = sorted(p for p in fs if p not in tests)
        lines = sum(fs[p][1] + fs[p][2] for p in srcs)
        alive = sorted(p for p in tests if fs[p][0] != "D")
        if (len(cm["parents"]) == 1 and srcs and grade.runnable(alive)
                and len(srcs) <= m["max_files"] and lines <= m["max_lines"]
                and not any(c.match(p, r["skip_if_changed"]) for p in fs)
                and (gh or len(cm["msg"].split()) >= m["min_statement_words"])):
            out.append({**cm, "tests": alive, "srcs": srcs, "lines": lines})
            if len(out) == m["max_candidates"]:
                break
    return out


def gh_api(root, path):
    try:
        r = subprocess.run(["gh", "api", path], cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def statement(root, sha, msg, min_words, gh):
    """(text, source): the linked issue, else the PR, else a long enough commit message."""
    if gh:
        prs = [p for p in gh_api(root, f"repos/{{owner}}/{{repo}}/commits/{sha}/pulls") or []
               if not p["title"].startswith("Merge ")]  # a merge-forward PR says nothing about the fix
        for n in FIXES.findall(" ".join([msg] + [p.get("body") or "" for p in prs])):
            issue = gh_api(root, f"repos/{{owner}}/{{repo}}/issues/{n}")
            if issue and "pull_request" not in issue:
                return f"{issue['title']}\n\n{issue.get('body') or ''}".strip(), "issue"
        if prs:
            return f"{prs[0]['title']}\n\n{prs[0].get('body') or ''}".strip(), "pr"
    if len(msg.split()) >= min_words:
        return msg, "commit"
    return None, None


def _outcome(rc0, before, rc1, after):
    """(verdict, why): per test when both runs have results, else by exit code."""
    if before is not None and after is not None:
        f2p = sorted(t for t, ok in after.items() if ok and not before.get(t))
        p2p = sorted(t for t, ok in after.items() if ok and before.get(t))
        if not f2p:
            return None, "tests fail after the fix" if not any(after.values()) else "tests pass before the fix"
        return (f2p, p2p), None
    if rc0 == 0:
        return None, "tests pass before the fix"
    if rc1 != 0:
        return None, "tests fail after the fix"
    return (None, None), None


def validate(cfg, cm, gh):
    """A task dict, or {"id", "reason", "log"} when the commit doesn't make a valid task."""
    root, m = cfg["root"], cfg["mine"]
    tid, fix, base = cm["sha"][:7], cm["sha"], cm["parents"][0]
    text, source = statement(root, fix, cm["msg"], m["min_statement_words"], gh)
    if not text:
        return {"id": tid, "reason": "no statement"}
    try:
        ws, box = workspace.create(cfg, tid, base)
    except RuntimeError as e:
        return {"id": tid, "reason": f"git: {e}"}
    log = ws.parent / "tests.log"

    def reject(reason, path=log):
        tail = path.read_text("utf-8", "replace")[-2000:] if path.exists() else ""
        return {"id": tid, "reason": reason, "log": tail}

    try:
        if not workspace.prepare(cfg, ws, box, ws.parent):
            return reject("setup failed", ws.parent / ("setup.log" if (ws.parent / "setup.log").exists()
                                                       else "install.log"))
        g.put(root, fix, cm["tests"], ws)
        files, mode, secs, seen = grade.runnable(cm["tests"]), "hidden", 0.0, None
        for i in range(m["verify_runs"]):
            g.put(root, base, cm["srcs"], ws)
            rc0, _, before = grade.test(cfg, box, ws, files, log)
            if i == 0 and LOAD_ERROR.search(log.read_text("utf-8", "replace")):
                mode = "visible"  # the tests can't even load: the fix adds names they use
            g.put(root, fix, cm["srcs"], ws)
            rc1, secs, after = grade.test(cfg, box, ws, files, log)
            verdict, why = _outcome(rc0, before, rc1, after)
            if why or (i and verdict != seen):
                return reject("flaky" if i else why)
            seen = verdict
        rc, _, suite = grade.test(cfg, box, ws, [], log, suite=True)
    finally:
        workspace.remove(ws, box)
    task = {"id": tid, "base": base, "fix": fix, "date": cm["date"], "statement": text, "statement_source": source,
            "source_files": cm["srcs"], "test_files": cm["tests"], "fix_lines": cm["lines"], "mode": mode,
            "suite_green": rc == 0, "test_secs": round(secs, 1)}
    if seen[0] is not None:  # per-test grading: what must flip, what must keep passing, what already failed
        task.update(fail_to_pass=seen[0], pass_to_pass=seen[1],
                    suite_failing=None if suite is None else sorted(t for t, ok in suite.items() if not ok))
    return task


def mine(cfg, log=print):
    """Validate candidates in parallel until max_tasks; resumable through tasks.jsonl and rejected.jsonl."""
    sb.STOP.clear()
    root, m, d = cfg["root"], cfg["mine"], store.state(cfg["root"])
    tasks = store.read(d / "tasks.jsonl")
    seen = {t["id"] for t in tasks + store.read(d / "rejected.jsonl")}
    gh = shutil.which("gh") is not None and gh_api(root, "repos/{owner}/{repo}") is not None
    found = candidates(cfg, gh)
    todo = iter([cm for cm in found if cm["sha"][:7] not in seen])
    need, jobs = m["max_tasks"] - len(tasks), cfg["run"]["jobs"]
    log(f"{len(found)} candidate commits, {len(tasks)} tasks mined before"
        + ("" if gh else " (gh unavailable: statements come from commit messages)"))
    with cf.ThreadPoolExecutor(jobs) as ex:
        running = {ex.submit(validate, cfg, cm, gh) for cm in itertools.islice(todo, jobs if need > 0 else 0)}
        try:
            while running:
                done, running = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                for f in done:
                    res = f.result()
                    if "reason" in res or need <= 0:
                        g.rmtree(d / "bases" / f"{res['id']}.git")
                    if "reason" in res:
                        store.append(d / "rejected.jsonl", res)
                        log(f"  skip {res['id']}: {res['reason']}")
                    elif need > 0:
                        store.append(d / "tasks.jsonl", res)
                        tasks.append(res)
                        need -= 1
                        graded = "per test" if res.get("fail_to_pass") else "by exit code"
                        log(f"  task {res['id']} ({res['mode']} tests, graded {graded}, "
                            f"statement from {res['statement_source']})")
                if need > 0:
                    running |= {ex.submit(validate, cfg, cm, gh) for cm in itertools.islice(todo, len(done))}
        except BaseException:
            sb.STOP.set()
            sb.killall()
            raise
    log(f"{len(tasks)} task{'s' * (len(tasks) != 1)} in {d / 'tasks.jsonl'}")
    return tasks
