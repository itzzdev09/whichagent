"""The matrix: tasks x agents x attempts, with jobs, timeouts, resume and a spend cap."""
import concurrent.futures as cf
import shutil
import statistics
import threading

from . import agents as ag, config as c, git as g, grade, sandbox as sb, store, workspace

NO_HISTORY = (0.10, 1.00)  # USD per run before an agent has any results: a deliberately wide guess
ERRORS = ("agent_error", "setup_error")


def prompt(task):
    text = f"Resolve the following task in this repository.\n\n{task['statement']}\n"
    if task["mode"] == "visible":
        text += ("\nThese tests fail now and must pass:\n"
                 + "".join(f"- {f}\n" for f in grade.runnable(task["test_files"])) + "Don't edit these files.\n")
    return text


def run_one(cfg, task, agent, attempt, keep=False):
    """One agent on one task in a fresh workspace, graded. Returns the result dict."""
    root = cfg["root"]
    argv = c.argv(agent)
    version = sb.version(cfg, argv[0])
    d = store.run_dir(root, task["id"], agent["name"], version, attempt)
    d.mkdir(parents=True, exist_ok=True)
    r = {"task": task["id"], "agent": agent["name"], "attempt": attempt, "status": "", "tests_pass": False,
         "regression": None, "cost_usd": None, "tokens_in": None, "tokens_out": None, "secs": None, "turns": None,
         "files_changed": 0, "extra_files": 0, "wrote_tests": False, "tampered": False, "flagged": [],
         "agent_version": version, "model": None}
    ws, box = workspace.create(cfg, task["id"], task["base"])
    try:
        exe = box.which(argv[0])
        if not exe:
            return {**r, "status": "agent_error", "error": f"{argv[0]} not found on PATH"}
        if not workspace.prepare(cfg, ws, box, d):
            return {**r, "status": "setup_error"}
        for dst, src in agent["overlay"].items():
            (ws / dst).parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree(root / src, ws / dst, dirs_exist_ok=True) if (root / src).is_dir()
             else shutil.copyfile(root / src, ws / dst))
        if task["mode"] == "visible":
            g.put(root, task["fix"], task["test_files"], ws)
        start = g.snapshot(ws)
        text = prompt(task)
        pf = ws.parent / "prompt.txt"  # outside the repo, so it isn't part of the agent's diff
        pf.write_text(text, "utf-8")
        (d / "prompt.txt").write_text(text, "utf-8")
        argv = [exe, *(a.replace("{prompt_file}", box.path(pf)) for a in argv[1:])]
        rc, secs = box.agent(argv, c.secs(agent["timeout"]), d / "stdout.log", d / "stderr.log", box.env(agent),
                             pf if agent["stdin"] else None)
        usage = ag.parse(cfg, agent["parse"], (d / "stdout.log").read_text("utf-8", "replace"), argv)
        r.update({k: v for k, v in usage.items() if k != "error"}, secs=round(secs, 1), exit_code=rc)
        try:
            r.update(grade.grade(cfg, task, ws, box, start, d))
        except RuntimeError as e:  # e.g. the agent broke the workspace's .git
            return {**r, "status": "agent_error", "error": str(e)}
        r["status"] = ("timeout" if rc is None else "solved" if r["tests_pass"] and not r["regression"]
                       else "agent_error" if rc or usage.get("error") else "failed")
        return r
    finally:
        if keep:
            box.close()
            print(f"  kept {ws}")
        else:
            workspace.remove(ws, box)


def run(cfg, agents, tasks, yes=False, keep=False, retry_errors=False, new_version=False, log=print):
    """Run every (task, agent, attempt) not yet in results.jsonl. Returns the new results.

    With new_version, a run done by another version of the agent doesn't count as done."""
    sb.STOP.clear()
    rc = cfg["run"]
    path = store.state(cfg["root"]) / "results.jsonl"
    old = store.read(path)
    versions = {a["name"]: sb.version(cfg, c.argv(a)[0]) for a in agents}

    def key(task_id, agent, attempt, version):
        return task_id, agent, attempt, version if new_version else None
    done = {key(x["task"], x["agent"], x["attempt"], x.get("agent_version")) for x in store.latest(old, new_version)
            if not (retry_errors and x["status"] in ERRORS)}
    todo = [(t, a, k) for t in tasks for k in range(1, rc["attempts"] + 1) for a in agents
            if key(t["id"], a["name"], k, versions[a["name"]]) not in done]  # interleaved: a stop stays comparable
    for a in agents:
        if a["parse"] not in ag.PARSERS:
            raise SystemExit(f"agent {a['name']!r}: parse must be one of {', '.join(ag.PARSERS)}")
        for src in a["overlay"].values():
            if not (cfg["root"] / src).exists():
                raise SystemExit(f"agent {a['name']!r}: overlay file {src} not found")
    if not todo:
        n = sum(x["status"] in ERRORS for x in store.latest(old))
        log("Nothing to run: every run is already in results.jsonl."
            + (f" {n} ended in an error; --retry-errors reruns them." if n else ""))
        return []
    costs = {a["name"]: [x["cost_usd"] for x in old if x["agent"] == a["name"] and x["cost_usd"] is not None
                         and x["status"] not in ERRORS] for a in agents}  # a failed login costs $0 and says nothing
    zero = {a["name"] for a in agents if ag.free(c.argv(a), a["env"]) or a["parse"] in ag.NO_COST}

    def per_run(name):  # (typical, worst) USD for one run: $0 for local and free models, else history, else a guess
        xs = costs[name]
        return (0.0, 0.0) if name in zero else (statistics.median(xs), max(xs)) if xs else NO_HISTORY
    lo = sum(per_run(a["name"])[0] for _, a, _ in todo)
    hi = sum(per_run(a["name"])[1] for _, a, _ in todo)
    log(f"{len(todo)} runs, estimated ${lo:.2f}-${hi:.2f} (stops before passing max_cost ${rc['max_cost']:.2f})")
    blind = [a["name"] for a in agents if a["parse"] in ag.NO_COST]
    if blind:
        log(f"No cost data from {', '.join(blind)}: their spending isn't counted toward max_cost.")
    if not yes and not c.ask("Continue?"):
        return []
    lock = threading.Lock()
    budget = {"spent": 0.0, "inflight": 0.0, "over": False}
    slots = {a["name"]: threading.Semaphore(rc["jobs_per_agent"] or rc["jobs"]) for a in agents}
    new = []

    def work(task, agent, attempt):
        # ponytail: a worker waiting on a busy agent's slot idles; a per-agent queue would keep it busy
        with slots[agent["name"]]:
            with lock:
                xs = costs[agent["name"]]
                est = 0.0 if agent["name"] in zero else sum(xs) / len(xs) if xs else NO_HISTORY[1]
                if sb.STOP.is_set() or budget["over"] or budget["spent"] + budget["inflight"] + est > rc["max_cost"]:
                    budget["over"] = not sb.STOP.is_set()
                    return
                budget["inflight"] += est
            try:
                r = run_one(cfg, task, agent, attempt, keep)
            except RuntimeError:
                if sb.STOP.is_set():
                    return  # interrupted: rerun on resume
                raise
            finally:
                with lock:
                    budget["inflight"] -= est
            if sb.STOP.is_set():
                return
            with lock:  # a run that ran but reported no cost (killed at its timeout, say) counts at its estimate
                budget["spent"] += r["cost_usd"] if r["cost_usd"] is not None else est if r["secs"] is not None else 0
                if r["cost_usd"] is not None:
                    xs.append(r["cost_usd"])
                store.append(path, r)
                new.append(r)
            money = "-" if r["cost_usd"] is None else f"${r['cost_usd']:.2f}"
            log(f"  {r['status']:<11} {task['id']}  {agent['name']} #{attempt}  {money}  {r['secs'] or 0:.0f}s")

    with cf.ThreadPoolExecutor(rc["jobs"]) as ex:
        futures = [ex.submit(work, *x) for x in todo]
        try:
            for f in cf.as_completed(futures):
                f.result()
        except BaseException:
            sb.STOP.set()
            sb.killall()
            raise
    if budget["over"]:
        log(f"Stopped at max_cost: spent ${budget['spent']:.2f}. Raise --max-cost and rerun to continue.")
    return new
