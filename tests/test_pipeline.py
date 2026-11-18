"""End to end on a fixture repo with fake agents: mine, run, grade, report. No API spend."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from tests import repos
from whichagent import cli, config, git, mine, run, store, workspace

HERE = Path(__file__).resolve().parent
FAKE = (HERE / "fake_agent.py").as_posix()
RUNNER = HERE / "junit_runner.py"
PY = Path(sys.executable).as_posix()
AGENTS = ["perfect", "noop", "tamper", "breaker", "sleeper", "peeker"]


def quiet(*_):
    pass


def docker_ok():
    """A running daemon with Linux containers (GitHub's Windows runners default to Windows containers)."""
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "linux"


def agent_toml(name, cmd, extra=""):
    return f"\n[[agent]]\nname = \"{name}\"\ncmd = '{cmd}'\nparse = \"claude-json\"\n{extra}"


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.repo = cls.tmp / "calc"
        cls.sha = repos.calc_repo(cls.repo)
        cls.state = cls.repo / ".whichagent"
        cls.beat = cls.tmp / "beat.txt"
        extra = {"sleeper": f"timeout = \"2s\"\nenv = {{ WA_BEAT = '{cls.beat.as_posix()}' }}\n"}
        runner = f'"{sys.executable}" "{RUNNER}" {{report}}'
        (cls.repo / "whichagent.toml").write_text(
            f"[repo]\ntest = '{runner} {{files}}'\nsuite = '{runner}'\n\n[run]\njobs = 3\ntimeout = \"60s\"\n"
            + "".join(agent_toml(a, f'"{PY}" "{FAKE}" {a}', extra.get(a, "")) for a in AGENTS), encoding="utf-8")
        os.environ["WA_SECRET"] = "must not reach agents"
        cls.cfg = config.load(cls.repo)
        cls.tasks = mine.mine(cls.cfg, log=quiet)
        cls.div = next(t for t in cls.tasks if t["fix"] == cls.sha["div"])
        new = run.run(cls.cfg, [cls.cfg["agents"][a] for a in AGENTS], cls.tasks, yes=True, log=quiet)
        cls.results = {(r["task"], r["agent"]): r for r in new}

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("WA_SECRET", None)
        git.rmtree(workspace.cache_dir(cls.cfg))
        git.rmtree(cls.tmp)

    def result(self, task, agent):
        return self.results[(self.sha[task][:7], agent)]

    def logs(self, task, agent):
        r = self.result(task, agent)
        return store.run_dir(self.repo, r["task"], r["agent"], r["agent_version"], r["attempt"])

    def test_mine(self):
        tasks = {t["id"]: t for t in self.tasks}
        self.assertEqual(set(tasks), {self.sha["div"][:7], self.sha["mod"][:7]})
        div, mod = tasks[self.sha["div"][:7]], tasks[self.sha["mod"][:7]]
        self.assertEqual((div["mode"], mod["mode"]), ("hidden", "visible"))
        self.assertEqual((div["source_files"], div["test_files"]), (["calc.py"], ["tests/test_calc.py"]))
        self.assertEqual((div["base"], div["statement_source"]), (self.sha["init"], "commit"))
        self.assertEqual(div["fail_to_pass"], ["unittest::tests.test_calc.T::test_div"])
        self.assertEqual(div["pass_to_pass"], ["unittest::tests.test_calc.T::test_add"])
        self.assertEqual((mod["fail_to_pass"], mod["pass_to_pass"]), (["unittest::tests.test_mod.T::test_mod"], []))
        self.assertEqual((div["suite_failing"], div["suite_green"]), ([], True))
        rejected = {r["id"]: r["reason"] for r in store.read(self.state / "rejected.jsonl")}
        self.assertEqual(rejected, {self.sha["passes"][:7]: "tests pass before the fix", self.sha["flaky"][:7]: "flaky"})
        self.assertEqual(sorted(p.name for p in (self.state / "bases").iterdir()), sorted(f"{i}.git" for i in tasks))
        self.assertEqual(git.git(self.repo, "branch", "--list", "whichagent/*"), "")
        self.assertEqual(mine.mine(self.cfg, log=quiet), self.tasks)  # cached: nothing revalidated

    def test_verdicts(self):
        expect = {"perfect": "solved", "noop": "failed", "tamper": "failed", "breaker": "failed",
                  "sleeper": "timeout", "peeker": "failed"}
        for task in ("div", "mod"):
            for agent, status in expect.items():
                with self.subTest(task=task, agent=agent):
                    self.assertEqual(self.result(task, agent)["status"], status)
        perfect, tamper, breaker = (self.result("div", a) for a in ("perfect", "tamper", "breaker"))
        self.assertEqual((perfect["files_changed"], perfect["extra_files"], perfect["tampered"]), (1, 0, False))
        self.assertEqual((perfect["cost_usd"], perfect["turns"], perfect["model"]), (0.01, 1, "fake-1"))
        self.assertEqual((perfect["f2p"], perfect["p2p"], perfect["regression"]), ("1/1", "1/1", False))
        self.assertTrue(tamper["tampered"])
        self.assertEqual((breaker["tests_pass"], breaker["regression"]), (True, True))
        self.assertEqual(breaker["broke"], ["unittest::tests.test_sub.T::test_sub"])
        self.assertTrue(self.result("mod", "tamper")["tampered"])
        self.assertIn("+    return a / b", (self.logs("div", "perfect") / "agent.diff").read_text())

    def test_peeker_sees_no_future(self):
        for task, future_code in (("div", "return a / b"), ("mod", "return a % b")):
            seen = json.loads((self.logs(task, "peeker") / "stderr.log").read_text().strip().splitlines()[-1])
            self.assertEqual(sorted(seen["log"]), sorted(git.git(self.repo, "rev-list", f"{self.sha[task]}~1").split()))
            self.assertNotIn(future_code, seen["found"])
            self.assertNotIn("WA_SECRET", seen["env"])

    def test_sleeper_leaves_no_process(self):
        before = self.beat.stat().st_mtime_ns
        time.sleep(0.5)
        self.assertEqual(self.beat.stat().st_mtime_ns, before)

    def test_exit_code_fallback(self):
        cfg = config.load(self.repo, {"repo": {"test": f'"{sys.executable}" -m unittest {{files}}', "suite": ""}})
        for agent, status in (("perfect", "solved"), ("noop", "failed")):
            r = run.run_one(cfg, self.div, cfg["agents"][agent], 90)
            self.assertEqual((r["status"], "f2p" in r), (status, False))

    def test_dependency_cache(self):
        count, install, check = self.tmp / "installs.txt", self.tmp / "install.py", self.tmp / "check.py"
        install.write_text("import os\nos.makedirs('deps', exist_ok=True)\nopen('deps/x.txt', 'w').write('1')\n"
                           f"open({str(count)!r}, 'a').write('.')\n")
        check.write_text("import os, sys\nsys.exit(0 if os.path.exists('deps/x.txt') else 1)\n")
        cfg = config.load(self.repo, {"repo": {"install": f'"{sys.executable}" "{install}"',
                                               "setup": f'"{sys.executable}" "{check}"', "cache": True}})
        for attempt in (91, 92):
            self.assertEqual(run.run_one(cfg, self.div, cfg["agents"]["noop"], attempt)["status"], "failed")
        self.assertEqual(count.read_text(), ".")  # installed once, restored from the cache once

    def test_regrades_when_the_tests_never_report(self):
        real = run.grade.grade

        def no_report_first(cfg, task, ws, box, start, run_dir):  # as when an agent's sandbox locks the workspace
            mock.side_effect = real
            out = real(cfg, task, ws, box, start, run_dir)
            return {**{k: v for k, v in out.items() if k not in ("f2p", "p2p")}, "tests_pass": False}
        with unittest.mock.patch.object(run.grade, "grade", side_effect=no_report_first) as mock:
            r = run.run_one(self.cfg, self.div, self.cfg["agents"]["perfect"], 98)
        self.assertEqual(mock.call_count, 2)
        self.assertEqual((r["status"], r["regraded"]), ("solved", True))

    def test_setup_error(self):
        cfg = config.load(self.repo, {"repo": {"setup": "exit 1"}})
        self.assertEqual(run.run_one(cfg, self.div, cfg["agents"]["noop"], 99)["status"], "setup_error")

    def test_retry_errors(self):
        noop = [self.cfg["agents"]["noop"]]
        broken = config.load(self.repo, {"repo": {"setup": "exit 1"}, "run": {"attempts": 2}})
        self.assertEqual({r["status"] for r in run.run(broken, noop, self.tasks, yes=True, log=quiet)}, {"setup_error"})
        fixed = config.load(self.repo, {"run": {"attempts": 2}})
        self.assertEqual(run.run(fixed, noop, self.tasks, yes=True, log=quiet), [])  # errors are kept by default
        retried = run.run(fixed, noop, self.tasks, yes=True, retry_errors=True, log=quiet)
        self.assertEqual([(r["attempt"], r["status"]) for r in retried], [(2, "failed")] * 2)
        latest = [r for r in store.latest(store.read(self.state / "results.jsonl")) if r["agent"] == "noop"]
        self.assertEqual(sorted(r["status"] for r in latest if r["attempt"] < 90), ["failed"] * 4)

    def test_resume_and_budget(self):
        perfect = [self.cfg["agents"]["perfect"]]
        self.assertEqual(run.run(self.cfg, perfect, self.tasks, yes=True, log=quiet), [])
        self.assertEqual(run.run(self.cfg, perfect, self.tasks, yes=True, new_version=True, log=quiet), [])
        cfg = config.load(self.repo, {"run": {"attempts": 5, "max_cost": 0.025, "jobs": 1}})
        self.assertEqual(len(run.run(cfg, perfect, self.tasks, yes=True, log=quiet)), 2)  # $0.01 each
        # an agent on a local model has no history, yet isn't held back by the $0.10-$1.00 guess for unknown agents
        local = {**self.cfg["agents"]["noop"], "name": "noop-local", "args": "--model ollama/x"}
        cfg = config.load(self.repo, {"run": {"max_cost": 0.5, "jobs": 1}})
        self.assertEqual(len(run.run(cfg, [local], self.tasks, yes=True, log=quiet)), len(self.tasks))
        # a paid agent whose runs report no cost counts at its $1.00 guess, not $0: max_cost still holds
        unpriced = {**self.cfg["agents"]["noop"], "name": "noop-unpriced", "parse": "opencode-json"}
        cfg = config.load(self.repo, {"run": {"max_cost": 1.5, "jobs": 1}})
        self.assertEqual(len(run.run(cfg, [unpriced], self.tasks, yes=True, log=quiet)), 1)

    def test_cli_report_show_compare(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for args in (["report"], ["show", self.sha["div"][:7]], ["compare", "perfect", "noop"],
                         ["report", "--by-version", "--baseline", str(self.state / "results.jsonl")]):
                self.assertEqual(cli.main(["-C", str(self.repo), *args]), 0)
        self.assertIn("Against the baseline", out.getvalue())
        page, md, svg = ((self.state / f).read_text("utf-8") for f in ("report.html", "report.md", "card.svg"))
        for name in AGENTS:
            self.assertIn(name, out.getvalue())
            self.assertIn(name, md)
            self.assertIn(name, page)
            self.assertIn(name, svg)

    @unittest.skipUnless(docker_ok(), "needs a running Docker")
    def test_docker_contains_agents(self):
        image_dir = self.tmp / "image"
        image_dir.mkdir()
        (image_dir / "Dockerfile").write_text("FROM python:3.12-slim\nRUN apt-get update && apt-get install -y"
                                              " --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n")
        subprocess.run(["docker", "build", "-q", "-t", "whichagent-test:1", str(image_dir)], check=True,
                       stdout=subprocess.DEVNULL)
        sentinel = self.tmp / "sentinel.txt"
        sentinel.write_text("outside the workspace")
        cfg = config.load(self.repo, {
            "run": {"sandbox": "docker"}, "docker": {"image": "whichagent-test:1", "mounts": [f"{HERE}:/fake:ro"]},
            "repo": {"test": "python /fake/junit_runner.py {report} {files}", "suite": "python /fake/junit_runner.py {report}"}})

        def agent(kind, env=None):
            return {**cfg["agents"]["noop"], "name": f"docker-{kind}", "cmd": f"python /fake/fake_agent.py {kind}",
                    "env": env or {}}
        self.assertEqual(run.run_one(cfg, self.div, agent("perfect"), 1)["status"], "solved")
        pwned = self.tmp / "pwned.txt"  # git runs on the host: a .git/config the agent wrote must not run there
        run.run_one(cfg, self.div, agent("hooker", {"WA_PWNED": pwned.as_posix()}), 1)
        self.assertFalse(pwned.exists())
        for kind, env in (("escaper", {"WA_SENTINEL": str(sentinel)}), ("peeker", {})):
            r = run.run_one(cfg, self.div, agent(kind, env), 1)
            seen = json.loads((store.run_dir(self.repo, r["task"], r["agent"], r["agent_version"], 1)
                               / "stderr.log").read_text().strip().splitlines()[-1])
            if kind == "escaper":
                self.assertIsNone(seen["read"])
            else:
                self.assertEqual(seen["log"], [self.sha["init"]])


if __name__ == "__main__":
    unittest.main()
