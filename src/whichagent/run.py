"""The matrix: tasks x agents x attempts, with jobs, timeouts, an env allowlist, resume and a spend cap."""
import concurrent.futures as cf
import os
import re
import shutil
import statistics
import threading

from . import agents as ag, config as c, git as g, grade

ENV_KEEP = set("""PATH PATHEXT HOME USER LOGNAME SHELL TERM LANG LANGUAGE TZ TMPDIR TMP TEMP
USERPROFILE HOMEDRIVE HOMEPATH APPDATA LOCALAPPDATA PROGRAMDATA ALLUSERSPROFILE PUBLIC
SYSTEMROOT SYSTEMDRIVE WINDIR COMSPEC OS USERNAME USERDOMAIN COMPUTERNAME NUMBER_OF_PROCESSORS
PROCESSOR_ARCHITECTURE PROGRAMFILES PROGRAMFILES(X86) PROGRAMW6432 COMMONPROGRAMFILES
COMMONPROGRAMFILES(X86) COMMONPROGRAMW6432 XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME
XDG_RUNTIME_DIR HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS
REQUESTS_CA_BUNDLE ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CONFIG_DIR
CLAUDE_CODE_GIT_BASH_PATH CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX AWS_PROFILE AWS_REGION
AWS_DEFAULT_REGION OPENAI_API_KEY OPENAI_BASE_URL CODEX_HOME GEMINI_API_KEY GOOGLE_API_KEY
GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_LOCATION GOOGLE_APPLICATION_CREDENTIALS GOOGLE_GENAI_USE_VERTEXAI
OPENROUTER_API_KEY DEEPSEEK_API_KEY MISTRAL_API_KEY GROQ_API_KEY XAI_API_KEY AZURE_OPENAI_API_KEY""".split())
NO_HISTORY = (0.10, 1.00)  # USD per run before an agent has any results: a deliberately wide guess


def agent_env(agent):
    """Only the allowlist from your shell, plus the agent's own env."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP or k.upper().startswith("LC_")}
    return {**env, **{k: str(v) for k, v in agent["env"].items()}}


def prompt(task):
    text = f"Resolve the following task in this repository.\n\n{task['statement']}\n"
    if task["mode"] == "visible":
        text += ("\nThese tests fail now and must pass:\n"
                 + "".join(f"- {f}\n" for f in grade.runnable(task["test_files"])) + "Don't edit these files.\n")
    return text


def slug(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "agent"


def run_dir(root, task_id, agent_name, attempt):
    return c.state(root) / "runs" / task_id / slug(agent_name) / str(attempt)


def run_one(cfg, task, agent, attempt, keep=False):
    """One agent on one task in a fresh workspace, graded. Returns the result dict."""
    root = cfg["root"]
    d = run_dir(root, task["id"], agent["name"], attempt)
    d.mkdir(parents=True, exist_ok=True)
    r = {"task": task["id"], "agent": agent["name"], "attempt": attempt, "status": "", "tests_pass": False,
         "regression": None, "cost_usd": None, "tokens_in": None, "tokens_out": None, "secs": None, "turns": None,
         "files_changed": 0, "extra_files": 0, "wrote_tests": False, "tampered": False, "flagged": [],
         "agent_version": None, "model": None}
    argv = c.argv(agent)
    exe = shutil.which(argv[0])
    if not exe:
        return {**r, "status": "agent_error", "error": f"{argv[0]} not found on PATH"}
    r["agent_version"] = ag.version(exe)
    ws = g.workspace(c.state(root) / "bases" / f"{task['id']}.git", cfg["branch"], root.name)
    try:
        rp = cfg["repo"]
        if rp["setup"]:
            if g.proc(rp["setup"], ws, c.secs(rp["setup_timeout"]), d / "setup.log", shell=True)[0] != 0:
                return {**r, "status": "setup_error"}
            g.exclude_untracked(ws)
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
        argv = [a.replace("{prompt_file}", str(pf)) for a in argv]
        rc, secs = g.proc([exe, *argv[1:]], ws, c.secs(agent["timeout"]), d / "stdout.log", err=d / "stderr.log",
                          env=agent_env(agent), stdin=pf if agent["stdin"] else None)
        usage = ag.parse(cfg, agent["parse"], (d / "stdout.log").read_text("utf-8", "replace"), argv)
        r.update({k: v for k, v in usage.items() if k != "error"}, secs=round(secs, 1), exit_code=rc)
        try:
            r.update(grade.grade(cfg, task, ws, start, d))
        except RuntimeError as e:  # e.g. the agent broke the workspace's .git
            return {**r, "status": "agent_error", "error": str(e)}
        r["status"] = ("timeout" if rc is None else "solved" if r["tests_pass"] and not r["regression"]
                       else "agent_error" if rc or usage.get("error") else "failed")
        return r
    finally:
        if keep:
            print(f"  kept {ws}")
        else:
            g.rmtree(ws.parent)


def run(cfg, agents, tasks, yes=False, keep=False, log=print):
    """Run every (task, agent, attempt) not yet in results.jsonl. Returns the new results."""
    g.STOP.clear()
    rc = cfg["run"]
    path = c.state(cfg["root"]) / "results.jsonl"
    old = c.read_jsonl(path)
    done = {(x["task"], x["agent"], x["attempt"]) for x in old}
    todo = [(t, a, k) for t in tasks for k in range(1, rc["attempts"] + 1) for a in agents
            if (t["id"], a["name"], k) not in done]  # interleaved task by task: a stop leaves agents comparable
    for a in agents:
        if a["parse"] not in ag.PARSERS:
            raise SystemExit(f"agent {a['name']!r}: parse must be one of {', '.join(ag.PARSERS)}")
        for src in a["overlay"].values():
            if not (cfg["root"] / src).exists():
                raise SystemExit(f"agent {a['name']!r}: overlay file {src} not found")
    if not todo:
        log("Nothing to run: every run is already in results.jsonl.")
        return []
    costs = {a["name"]: [x["cost_usd"] for x in old if x["agent"] == a["name"] and x["cost_usd"] is not None]
             for a in agents}
    lo = sum(statistics.median(costs[a["name"]]) if costs[a["name"]] else NO_HISTORY[0] for _, a, _ in todo)
    hi = sum(max(costs[a["name"]]) if costs[a["name"]] else NO_HISTORY[1] for _, a, _ in todo)
    log(f"{len(todo)} runs, estimated ${lo:.2f}-${hi:.2f} (stops before passing max_cost ${rc['max_cost']:.2f})")
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
                est = sum(xs) / len(xs) if xs else NO_HISTORY[1]
                if g.STOP.is_set() or budget["over"] or budget["spent"] + budget["inflight"] + est > rc["max_cost"]:
                    budget["over"] = not g.STOP.is_set()
                    return
                budget["inflight"] += est
            try:
                r = run_one(cfg, task, agent, attempt, keep)
            except RuntimeError:
                if g.STOP.is_set():
                    return  # interrupted: rerun on resume
                raise
            finally:
                with lock:
                    budget["inflight"] -= est
            if g.STOP.is_set():
                return
            with lock:
                budget["spent"] += r["cost_usd"] or 0
                if r["cost_usd"] is not None:
                    xs.append(r["cost_usd"])
                c.append_jsonl(path, r)
                new.append(r)
            money = "-" if r["cost_usd"] is None else f"${r['cost_usd']:.2f}"
            log(f"  {r['status']:<11} {task['id']}  {agent['name']} #{attempt}  {money}  {r['secs'] or 0:.0f}s")

    with cf.ThreadPoolExecutor(rc["jobs"]) as ex:
        futures = [ex.submit(work, *x) for x in todo]
        try:
            for f in cf.as_completed(futures):
                f.result()
        except BaseException:
            g.STOP.set()
            g.killall()
            raise
    if budget["over"]:
        log(f"Stopped at max_cost: spent ${budget['spent']:.2f}. Raise --max-cost and rerun to continue.")
    return new
