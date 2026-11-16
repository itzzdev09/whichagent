"""End to end on a fixture repo with fake agents: mine, run, grade, report. No API spend."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import repos
from whichagent import cli, config, git, mine, run

FAKE = Path(__file__).with_name("fake_agent.py").as_posix()
PY = Path(sys.executable).as_posix()
AGENTS = ["perfect", "noop", "tamper", "breaker", "sleeper", "peeker"]


def quiet(*_):
    pass


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.repo = cls.tmp / "calc"
        cls.sha = repos.calc_repo(cls.repo)
        cls.state = cls.repo / ".whichagent"
        cls.beat = cls.tmp / "beat.txt"
        extra = {"sleeper": f"timeout = \"2s\"\nenv = {{ WA_BEAT = '{cls.beat.as_posix()}' }}\n"}
        (cls.repo / "whichagent.toml").write_text(
            f"[repo]\ntest = '\"{sys.executable}\" -m unittest {{files}}'\n\n[run]\njobs = 3\ntimeout = \"60s\"\n"
            + "".join(f"\n[[agent]]\nname = \"{a}\"\ncmd = '\"{PY}\" \"{FAKE}\" {a}'\nparse = \"claude-json\"\n"
                      + extra.get(a, "") for a in AGENTS), encoding="utf-8")
        os.environ["WA_SECRET"] = "must not reach agents"
        cls.cfg = config.load(cls.repo)
        cls.tasks = mine.mine(cls.cfg, log=quiet)
        new = run.run(cls.cfg, [cls.cfg["agents"][a] for a in AGENTS], cls.tasks, yes=True, log=quiet)
        cls.results = {(r["task"], r["agent"]): r for r in new}

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("WA_SECRET", None)
        git.rmtree(cls.tmp)

    def result(self, task, agent):
        return self.results[(self.sha[task][:7], agent)]

    def test_mine(self):
        tasks = {t["id"]: t for t in self.tasks}
        self.assertEqual(set(tasks), {self.sha["div"][:7], self.sha["mod"][:7]})
        div, mod = tasks[self.sha["div"][:7]], tasks[self.sha["mod"][:7]]
        self.assertEqual((div["mode"], mod["mode"]), ("hidden", "visible"))
        self.assertEqual((div["source_files"], div["test_files"]), (["calc.py"], ["tests/test_calc.py"]))
        self.assertEqual((div["base"], div["statement_source"]), (self.sha["init"], "commit"))
        self.assertTrue(div["suite_green"] and mod["suite_green"])
        rejected = {r["id"]: r["reason"] for r in config.read_jsonl(self.state / "rejected.jsonl")}
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
        self.assertTrue(tamper["tampered"])
        self.assertEqual((breaker["tests_pass"], breaker["regression"]), (True, True))
        self.assertTrue(self.result("mod", "tamper")["tampered"])
        diff = (self.state / "runs" / self.sha["div"][:7] / "perfect" / "1" / "agent.diff").read_text()
        self.assertIn("+    return a / b", diff)

    def test_peeker_sees_no_future(self):
        for task, future_code in (("div", "return a / b"), ("mod", "return a % b")):
            log = self.state / "runs" / self.sha[task][:7] / "peeker" / "1" / "stderr.log"
            seen = json.loads(log.read_text().strip().splitlines()[-1])
            self.assertEqual(sorted(seen["log"]), sorted(git.git(self.repo, "rev-list", f"{self.sha[task]}~1").split()))
            self.assertNotIn(future_code, seen["found"])
            self.assertNotIn("WA_SECRET", seen["env"])

    def test_sleeper_leaves_no_process(self):
        before = self.beat.stat().st_mtime_ns
        time.sleep(0.5)
        self.assertEqual(self.beat.stat().st_mtime_ns, before)

    def test_setup_error(self):
        cfg = config.load(self.repo, {"repo": {"setup": "exit 1"}})
        self.assertEqual(run.run_one(cfg, self.tasks[0], cfg["agents"]["noop"], 99)["status"], "setup_error")

    def test_retry_errors(self):
        noop = [self.cfg["agents"]["noop"]]
        broken = config.load(self.repo, {"repo": {"setup": "exit 1"}, "run": {"attempts": 2}})
        self.assertEqual({r["status"] for r in run.run(broken, noop, self.tasks, yes=True, log=quiet)}, {"setup_error"})
        fixed = config.load(self.repo, {"run": {"attempts": 2}})
        self.assertEqual(run.run(fixed, noop, self.tasks, yes=True, log=quiet), [])  # errors are kept by default
        retried = run.run(fixed, noop, self.tasks, yes=True, retry_errors=True, log=quiet)
        self.assertEqual([(r["attempt"], r["status"]) for r in retried], [(2, "failed")] * 2)
        latest = [r for r in config.latest(config.read_jsonl(self.state / "results.jsonl")) if r["agent"] == "noop"]
        self.assertEqual(sorted(r["status"] for r in latest), ["failed"] * 4)

    def test_resume_and_budget(self):
        perfect = [self.cfg["agents"]["perfect"]]
        self.assertEqual(run.run(self.cfg, perfect, self.tasks, yes=True, log=quiet), [])
        cfg = config.load(self.repo, {"run": {"attempts": 5, "max_cost": 0.025, "jobs": 1}})
        self.assertEqual(len(run.run(cfg, perfect, self.tasks, yes=True, log=quiet)), 2)  # $0.01 each

    def test_cli_report_show_compare(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for args in (["report"], ["show", self.sha["div"][:7]], ["compare", "perfect", "noop"]):
                self.assertEqual(cli.main(["-C", str(self.repo), *args]), 0)
        page = (self.state / "report.html").read_text("utf-8")
        md = (self.state / "report.md").read_text("utf-8")
        for name in AGENTS:
            self.assertIn(name, out.getvalue())
            self.assertIn(name, md)
            self.assertIn(name, page)


if __name__ == "__main__":
    unittest.main()
