"""whichagent command line: quickstart, init, doctor, mine, run, report, compare, show."""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, agents as ag, config as c, git as g, grade, mine, report, run, stats

SAFETY = """whichagent runs coding agents unattended, with their permission prompts off:
  - each run happens in a throwaway clone in your temp dir; your working tree isn't touched
  - agents get an allowlist of environment variables, not your whole shell
  - runs are killed at their timeout, and no run starts that would pass max_cost
This is not a sandbox. An agent can still read and write whatever your user account can, and reach
the network. Docker isolation is planned for v0.3."""


def _list(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", dest="repo", metavar="DIR", default=argparse.SUPPRESS,
                        help="the repo to benchmark (default: current directory)")
    common.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="don't ask to confirm")
    common.add_argument("--keep", action="store_true", default=argparse.SUPPRESS,
                        help="keep agent workspaces for inspection")
    for sec, keys in c.DEFAULTS.items():  # every config key is also a flag: --max-tasks 40
        for key, v in keys.items():
            kw = {"dest": f"{sec}.{key}", "default": argparse.SUPPRESS, "help": f"[{sec}] {key}"}
            if isinstance(v, bool):
                common.add_argument("--" + key.replace("_", "-"), action=argparse.BooleanOptionalAction, **kw)
            else:
                common.add_argument("--" + key.replace("_", "-"), type=_list if isinstance(v, list) else type(v), **kw)
    p = argparse.ArgumentParser(prog="whichagent", parents=[common],
                                description="Find out which coding agent actually works on your repo.")
    p.add_argument("--version", action="version", version=f"whichagent {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    for name, text in [("init", "write whichagent.toml from the detected stack"),
                       ("doctor", "check git, the test command at HEAD and each agent's flags"),
                       ("mine", "find and validate tasks -> .whichagent/tasks.jsonl"),
                       ("run", "run the matrix (resumable) -> .whichagent/results.jsonl"),
                       ("report", "terminal table, report.html, report.md")]:
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
        g.STOP.set()
        g.killall()
        print("\nStopped. Run the same command again to resume.")
        return 130


def init(cfg, a):
    root, setup, test = cfg["root"], cfg["repo"]["setup"], cfg["repo"]["test"]
    if not test:
        if a.get("yes") or not sys.stdin.isatty():
            raise SystemExit("Can't detect the stack: pass --setup and --test, e.g. --test 'pytest -q {files}'")
        setup = input("Setup command (empty for none): ").strip()
        test = input("Test command, with {files} where the test files go: ").strip()
        if "{files}" not in test:
            raise SystemExit("The test command needs {files}")
    c.write_template(root, setup, test)
    print(f"Wrote whichagent.toml: setup = {setup!r}, test = {test!r}")


def _need_test(cfg):
    if not cfg["repo"]["test"]:
        raise SystemExit("No test command: run `whichagent init` or pass --test")


def _data(cfg):
    d = c.state(cfg["root"])
    return c.read_jsonl(d / "tasks.jsonl"), c.read_jsonl(d / "results.jsonl")


def cmd_quickstart(cfg, a):
    cmd_mine(cfg, a)
    return cmd_run(cfg, a)


def cmd_doctor(cfg, a):
    root, ok = cfg["root"], True
    print(g.git(root, "--version").strip())
    if not cfg["repo"]["test"]:
        print("test command: not set (run `whichagent init`)")
        ok = False
    else:
        tmp = Path(tempfile.mkdtemp())
        ws, log = tmp / root.name, c.state(root) / "doctor.log"
        try:
            g.git(tmp, "clone", "-q", "--depth=1", root.as_uri(), str(ws))
            if cfg["repo"]["setup"] and g.proc(cfg["repo"]["setup"], ws, c.secs(cfg["repo"]["setup_timeout"]),
                                               log, shell=True)[0] != 0:
                print(f"setup at HEAD: FAILED, see {log}")
                ok = False
            else:
                rc, secs = grade.test(cfg, ws, [], log)
                print(f"test suite at HEAD: {'passes' if rc == 0 else 'FAILS'} ({secs:.0f}s, log in {log})")
                ok = ok and rc == 0
        finally:
            g.rmtree(tmp)
    for name, agent in cfg["agents"].items():
        argv = c.argv(agent)
        exe = shutil.which(argv[0])
        note = " (unverified adapter)" if agent.get("unverified") else ""
        if not exe:
            print(f"{name}: {argv[0]} not found{note}")
            continue
        missing = ag.missing_flags(exe, argv)
        print(f"{name}: {ag.version(exe)}, " + (f"flags not in --help: {' '.join(missing)}" if missing
                                                 else "flags ok") + note)
    return 0 if ok else 1


def cmd_mine(cfg, a):
    _need_test(cfg)
    mine.mine(cfg)


def cmd_run(cfg, a):
    _need_test(cfg)
    tasks, _ = _data(cfg)
    if not tasks:
        raise SystemExit("No tasks: run `whichagent mine` first (or check .whichagent/rejected.jsonl)")
    agents = c.selected(cfg)
    if not agents:
        raise SystemExit("No agents: none of claude, codex, gemini, aider is on PATH and whichagent.toml defines none")
    print("Agents: " + ", ".join(x["name"] + (" (unverified adapter)" if x.get("unverified") else "") for x in agents))
    mark = Path.home() / ".whichagent" / "accepted"
    if not mark.exists():
        print(SAFETY)
        if not a.get("yes"):
            if not c.ask("Continue?"):
                return 1
            mark.parent.mkdir(exist_ok=True)
            mark.write_text("yes\n")
    run.run(cfg, agents, tasks, yes=a.get("yes", False), keep=a.get("keep", False))
    return cmd_report(cfg, a)


def cmd_report(cfg, a):
    tasks, results = _data(cfg)
    if not results:
        raise SystemExit("No results yet: run `whichagent run` first")
    d = c.state(cfg["root"])
    print(report.terminal(tasks, results))
    (d / "report.md").write_text(report.markdown(tasks, results, cfg["root"].name), "utf-8")
    (d / "report.html").write_text(report.page(cfg, tasks, results), "utf-8")
    print(f"\nReport: {d / 'report.html'}")


def cmd_compare(cfg, a):
    _, results = _data(cfg)
    ra, rb = ([r for r in results if r["agent"] == n] for n in (a["a"], a["b"]))
    if not ra or not rb:
        raise SystemExit(f"No results for {a['a'] if not ra else a['b']}")
    print("  ".join(report._verdict(a["a"], a["b"], *stats.compare(ra, rb))))


def cmd_show(cfg, a):
    tasks, results = _data(cfg)
    t = next((t for t in tasks if t["id"].startswith(a["task"])), None)
    if not t:
        raise SystemExit(f"No task {a['task']}")
    print(f"Task {t['id']} ({t['mode']} tests, statement from {t['statement_source']})\n\n{t['statement']}\n")
    print("=== Real fix\n" + g.git(cfg["root"], "show", "--format=", "--no-color", "--no-ext-diff", t["fix"]))
    for r in sorted((r for r in results if r["task"] == t["id"]), key=lambda r: (r["agent"], r["attempt"])):
        f = run.run_dir(cfg["root"], t["id"], r["agent"], r["attempt"]) / "agent.diff"
        print(f"=== {r['agent']}, attempt {r['attempt']}: {r['status']}\n"
              + (f.read_text("utf-8", "replace") if f.exists() else "(no diff)\n"))


COMMANDS = {"quickstart": cmd_quickstart, "doctor": cmd_doctor, "mine": cmd_mine, "run": cmd_run,
            "report": cmd_report, "compare": cmd_compare, "show": cmd_show}
