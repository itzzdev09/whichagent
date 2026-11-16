"""Find fix commits that came with tests; keep the ones whose tests fail before the fix and pass after."""
import concurrent.futures as cf
import itertools
import json
import re
import shutil
import subprocess

from . import config as c, git as g, grade

LOAD_ERROR = re.compile(r"ImportError|ModuleNotFoundError|errors? during collection|Cannot find module"
                        r"|does not provide an export named")
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


def validate(cfg, cm, gh):
    """A task dict, or {"id", "reason", "log"} when the commit doesn't make a valid task."""
    root, m, rp = cfg["root"], cfg["mine"], cfg["repo"]
    tid, fix, base = cm["sha"][:7], cm["sha"], cm["parents"][0]
    text, source = statement(root, fix, cm["msg"], m["min_statement_words"], gh)
    if not text:
        return {"id": tid, "reason": "no statement"}
    try:
        bare = g.base_clone(root, base, c.state(root) / "bases" / f"{tid}.git", m["history_depth"])
        ws = g.workspace(bare, cfg["branch"], root.name)
    except RuntimeError as e:
        return {"id": tid, "reason": f"git: {e}"}
    log = ws.parent / "tests.log"

    def reject(reason):
        return {"id": tid, "reason": reason, "log": log.read_text("utf-8", "replace")[-2000:]}

    try:
        if rp["setup"] and g.proc(rp["setup"], ws, c.secs(rp["setup_timeout"]), log, shell=True)[0] != 0:
            return reject("setup failed")
        g.put(root, fix, cm["tests"], ws)
        files, mode, secs = grade.runnable(cm["tests"]), "hidden", 0.0
        for i in range(m["verify_runs"]):
            g.put(root, base, cm["srcs"], ws)
            if grade.test(cfg, ws, files, log)[0] == 0:
                return reject("flaky" if i else "tests pass before the fix")
            if i == 0 and LOAD_ERROR.search(log.read_text("utf-8", "replace")):
                mode = "visible"  # the tests can't even load: the fix adds names they import
            g.put(root, fix, cm["srcs"], ws)
            rc, secs = grade.test(cfg, ws, files, log)
            if rc != 0:
                return reject("flaky" if i else "tests fail after the fix")
        green = grade.test(cfg, ws, [], log)[0] == 0
    finally:
        g.rmtree(ws.parent)
    return {"id": tid, "base": base, "fix": fix, "date": cm["date"], "statement": text,
            "statement_source": source, "source_files": cm["srcs"], "test_files": cm["tests"],
            "fix_lines": cm["lines"], "mode": mode, "suite_green": green, "test_secs": round(secs, 1)}


def mine(cfg, log=print):
    """Validate candidates in parallel until max_tasks; resumable through tasks.jsonl and rejected.jsonl."""
    g.STOP.clear()
    root, m, d = cfg["root"], cfg["mine"], c.state(cfg["root"])
    tasks = c.read_jsonl(d / "tasks.jsonl")
    seen = {t["id"] for t in tasks + c.read_jsonl(d / "rejected.jsonl")}
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
                        c.append_jsonl(d / "rejected.jsonl", res)
                        log(f"  skip {res['id']}: {res['reason']}")
                    elif need > 0:
                        c.append_jsonl(d / "tasks.jsonl", res)
                        tasks.append(res)
                        need -= 1
                        log(f"  task {res['id']} ({res['mode']} tests, statement from {res['statement_source']})")
                if need > 0:
                    running |= {ex.submit(validate, cfg, cm, gh) for cm in itertools.islice(todo, len(done))}
        except BaseException:
            g.STOP.set()
            g.killall()
            raise
    log(f"{len(tasks)} task{'s' * (len(tasks) != 1)} in {d / 'tasks.jsonl'}")
    return tasks
