"""whichagent command line: quickstart, init, doctor, mine, run, report, compare, show."""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, agents as ag, config as c, git as g, grade, mine, report, run, sandbox as sb, stats
from . import store, workspace

SAFETY = """whichagent runs coding agents unattended, with their permission prompts off:
  - each run happens in a throwaway clone in your temp dir; your working tree isn't touched
  - agents get an allowlist of environment variables, not your whole shell
  - runs are killed at their timeout, and no run starts that would pass max_cost
On the host this is not a sandbox: an agent can still read and write whatever your user account can,
and reach the network. --sandbox docker runs setup, agents and tests in a container that sees only the workspace."""


def _list(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def parser():
    common = argparse.ArgumentParser(add_help=False)
    flag = common.add_argument
    flag("-C", dest="repo", metavar="DIR", default=argparse.SUPPRESS, help="the repo (default: current directory)")
    flag("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="don't ask to confirm")
    flag("--keep", action="store_true", default=argparse.SUPPRESS, help="keep agent workspaces for inspection")
    flag("--retry-errors", action="store_true", default=argparse.SUPPRESS,
         help="rerun runs that ended in agent_error or setup_error (e.g. after fixing a login)")
    flag("--new-version", action="store_true", default=argparse.SUPPRESS,
         help="rerun tasks an agent has only done with another version of itself")
    flag("--by-version", action="store_true", default=argparse.SUPPRESS,
         help="report each agent version separately")
    flag("--tasks", metavar="IDS", type=_list, default=argparse.SUPPRESS,
         help="only these tasks (ids or id prefixes, comma-separated), e.g. to try an agent on a free tier")
    flag("--baseline", metavar="PATH", default=argparse.SUPPRESS,
         help="compare against another results.jsonl (or a dir holding .whichagent/)")
    for sec, keys in c.DEFAULTS.items():  # every config key is also a flag: --max-tasks 40
        for key, v in keys.items():
            kw = {"dest": f"{sec}.{key}", "default": argparse.SUPPRESS, "help": f"[{sec}] {key}"}
            if isinstance(v, bool):
                flag("--" + key.replace("_", "-"), action=argparse.BooleanOptionalAction, **kw)
            else:
                flag("--" + key.replace("_", "-"), type=_list if isinstance(v, list) else type(v), **kw)
    p = argparse.ArgumentParser(prog="whichagent", parents=[common],
                                description="Find out which coding agent actually works on your repo.")
    p.add_argument("--version", action="version", version=f"whichagent {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    for name, text in [("init", "write whichagent.toml from the detected stack"),
                       ("doctor", "check git, the test command at HEAD and each agent"),
                       ("mine", "find and validate tasks -> .whichagent/tasks.jsonl"),
                       ("run", "run the matrix (resumable) -> .whichagent/results.jsonl"),
                       ("report", "terminal table, report.html, report.md, card.svg")]:
        sub.add_parser(name, parents=[common], help=text)
    s = sub.add_parser("compare", parents=[common], help="paired comparison of two agents")
    s.add_argument("a")
    s.add_argument("b")
    s = sub.add_parser("show", parents=[common], help="statement, real fix, each agent's diff")
    s.add_argument("task")
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a cp1252 pipe on Windows can't print everything
    a = vars(parser().parse_args(argv))
    root = Path(a.get("repo", ".")).resolve()
    over = {}
    for k, v in a.items():
        if "." in k:
            sec, key = k.split(".")
            over.setdefault(sec, {})[key] = v
    try:
        g.git(root, "rev-parse", "--git-dir")
    except RuntimeError:
        raise SystemExit(f"{root} isn't a git repository")
    cmd = a.get("cmd") or "quickstart"
    try:
        if cmd in ("init", "quickstart") and not (root / "whichagent.toml").exists():
            init(c.load(root, over), a)
        elif cmd == "init":
            print("whichagent.toml already exists")
        if cmd == "init":
            return 0
        cfg = c.load(root, over)
        return COMMANDS[cmd](cfg, a) or 0
    except KeyboardInterrupt:
        sb.STOP.set()
        sb.killall()
        print("\nStopped. Run the same command again to resume.")
        return 130


def init(cfg, a):
    rp = cfg["repo"]
    if not rp["test"]:
        if a.get("yes") or not sys.stdin.isatty():
            raise SystemExit("Can't detect the stack: pass --setup and --test, e.g. --test 'pytest -q {files}'")
        rp["setup"] = input("Setup command (empty for none): ").strip()
        rp["test"] = input("Test command, with {files} where the test files go: ").strip()
        if not c.PLACEHOLDER.search(rp["test"]):
            raise SystemExit("The test command needs {files}")
    c.write_template(cfg["root"], rp)
    print("Wrote whichagent.toml: " + ", ".join(f"{k} = {rp[k]!r}" for k in ("install", "setup", "test") if rp[k]))


def _need_test(cfg):
    if not cfg["repo"]["test"]:
        raise SystemExit("No test command: run `whichagent init` or pass --test")


def _data(cfg, a):
    d = store.state(cfg["root"])
    return store.read(d / "tasks.jsonl"), store.latest(store.read(d / "results.jsonl"), a.get("by_version", False))


def _baseline(a):
    if not a.get("baseline"):
        return None
    p = Path(a["baseline"])
    for f in (p, p / "results.jsonl", p / ".whichagent" / "results.jsonl"):
        if f.is_file():
            return store.latest(store.read(f), a.get("by_version", False))
    raise SystemExit(f"No results.jsonl at {p}")


def cmd_quickstart(cfg, a):
    cmd_mine(cfg, a)
    return cmd_run(cfg, a)


def cmd_doctor(cfg, a):
    root, ok = cfg["root"], True
    print(g.git(root, "--version").strip())
    if cfg["run"]["sandbox"] == "docker":
        if not shutil.which("docker"):
            raise SystemExit("docker mode: docker isn't on PATH")
        print(f"docker image: {sb.image(root, cfg['docker']['image'])}")
    if not cfg["repo"]["test"]:
        print("test command: not set (run `whichagent init`)")
        ok = False
    else:
        tmp = Path(tempfile.mkdtemp())
        ws, logs = tmp / root.name, store.state(root)
        g.git(tmp, "clone", "-q", "--depth=1", root.as_uri(), str(ws))
        box = sb.executor(cfg, ws)
        try:
            if not workspace.prepare(cfg, ws, box, logs):
                print(f"install or setup at HEAD: FAILED, see {logs / 'install.log'} and {logs / 'setup.log'}")
                ok = False
            else:
                rc, secs, results = grade.test(cfg, box, ws, [], logs / "doctor.log", suite=True)
                graded = (f"{len(results)} tests with per-test results" if results
                          else "no per-test results, so tasks are graded by exit code")
                print(f"test suite at HEAD: {'passes' if rc == 0 else 'FAILS'} ({secs:.0f}s, {graded}, "
                      f"log in {logs / 'doctor.log'})")
                if rc != 0:
                    tail = (logs / "doctor.log").read_text("utf-8", "replace").strip().splitlines()[-8:]
                    print("  " + "\n  ".join(tail))
                ok = ok and rc == 0
        finally:
            workspace.remove(ws, box)
    for name, agent in cfg["agents"].items():
        argv = c.argv(agent)
        note = " (unverified adapter)" if agent.get("unverified") else ""
        version = sb.version(cfg, argv[0])
        if not version:
            print(f"{name}: {argv[0]} not found{note}")
        elif cfg["run"]["sandbox"] == "docker":
            print(f"{name}: {version} (in the image){note}")
        else:
            missing = ag.missing_flags(shutil.which(argv[0]), argv)
            print(f"{name}: {version}, " + (f"flags not in --help: {' '.join(missing)}" if missing
                                             else "flags ok") + note)
    return 0 if ok else 1


def cmd_mine(cfg, a):
    _need_test(cfg)
    mine.mine(cfg)


def cmd_run(cfg, a):
    _need_test(cfg)
    tasks = store.read(store.state(cfg["root"]) / "tasks.jsonl")
    if not tasks:
        raise SystemExit("No tasks: run `whichagent mine` first (or check .whichagent/rejected.jsonl)")
    if a.get("tasks"):
        tasks = [t for t in tasks if any(t["id"].startswith(x) for x in a["tasks"])] or sys.exit("No such tasks")
    agents = c.selected(cfg)
    if not agents:
        raise SystemExit("No agents: none of the built-ins is on PATH and whichagent.toml defines none")
    print("Agents: " + ", ".join(x["name"] + (" (unverified adapter)" if x.get("unverified") else "") for x in agents))
    mark = Path.home() / ".whichagent" / "accepted"
    if not mark.exists():
        print(SAFETY)
        if not a.get("yes"):
            if not c.ask("Continue?"):
                return 1
            mark.parent.mkdir(exist_ok=True)
            mark.write_text("yes\n")
    run.run(cfg, agents, tasks, yes=a.get("yes", False), keep=a.get("keep", False),
            retry_errors=a.get("retry_errors", False), new_version=a.get("new_version", False))
    return cmd_report(cfg, a)


def cmd_report(cfg, a):
    tasks, results = _data(cfg, a)
    if not results:
        raise SystemExit("No results yet: run `whichagent run` first")
    d, name, by, base = store.state(cfg["root"]), cfg["root"].name, a.get("by_version", False), _baseline(a)
    print(report.terminal(tasks, results, base, by))
    (d / "report.md").write_text(report.markdown(tasks, results, name, base, by), "utf-8")
    (d / "report.html").write_text(report.page(cfg, tasks, results, by), "utf-8")
    (d / "card.svg").write_text(report.card(tasks, results, name, by), "utf-8")
    print(f"\nReport: {d / 'report.html'} (also report.md and card.svg)")


def cmd_compare(cfg, a):
    _, results = _data(cfg, a)
    by = a.get("by_version", False)
    ra, rb = ([r for r in results if report.label(r, by) == n] for n in (a["a"], a["b"]))
    if not ra or not rb:
        raise SystemExit(f"No results for {a['a'] if not ra else a['b']}")
    print("  ".join(report._verdict(a["a"], a["b"], *stats.compare(ra, rb))))


def cmd_show(cfg, a):
    tasks, results = _data(cfg, a)
    t = next((t for t in tasks if t["id"].startswith(a["task"])), None)
    if not t:
        raise SystemExit(f"No task {a['task']}")
    graded = f"{len(t['fail_to_pass'])} tests must flip" if t.get("fail_to_pass") else "graded by exit code"
    print(f"Task {t['id']} ({t['mode']} tests, {graded}, statement from {t['statement_source']})\n\n{t['statement']}\n")
    print("=== Real fix\n" + g.git(cfg["root"], "show", "--format=", "--no-color", "--no-ext-diff", t["fix"]))
    for r in sorted((r for r in results if r["task"] == t["id"]), key=lambda r: (r["agent"], r["attempt"])):
        f = store.run_dir(cfg["root"], t["id"], r["agent"], r.get("agent_version"), r["attempt"]) / "agent.diff"
        print(f"=== {r['agent']}, attempt {r['attempt']}: {r['status']}\n"
              + (f.read_text("utf-8", "replace") if f.exists() else "(no diff)\n"))


COMMANDS = {"quickstart": cmd_quickstart, "doctor": cmd_doctor, "mine": cmd_mine, "run": cmd_run,
            "report": cmd_report, "compare": cmd_compare, "show": cmd_show}
