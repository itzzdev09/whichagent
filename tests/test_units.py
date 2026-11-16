import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import tests  # noqa: F401  (puts src/ on sys.path)
from whichagent import agents, config, grade, report, sandbox, stacks, stats, store, testresults

CFG = {"root": Path("."), "prices": {"gpt-x": {"input": 1.0, "output": 10.0, "cache_read": 0.1},
                                     "gem-x": {"input": 2.0, "output": 20.0}}}


def result(task, agent, version, status, cost=0.1):
    return {"task": task, "agent": agent, "agent_version": version, "attempt": 1, "status": status,
            "cost_usd": cost, "secs": 10.0, "tampered": False, "regression": None, "flagged": []}


class Units(unittest.TestCase):
    def test_patterns(self):
        cfg = {"repo": config.DEFAULTS["repo"]}
        for p in ["tests/test_calc.py", "pkg/test_x.py", "src/a.test.ts", "src/__tests__/a.js", "conftest.py",
                  "src/a.test-d.ts", "pkg/x_test.go", "core/src/test/java/a/FooTest.java"]:
            self.assertTrue(config.is_test(cfg, p), p)
        for p in ["calc.py", "src/testing.py", "docs/tests.md", "src/main/java/a/Foo.java"]:
            self.assertFalse(config.is_test(cfg, p), p)
        skip = config.DEFAULTS["repo"]["skip_if_changed"]
        self.assertTrue(config.match("requirements-dev.txt", skip) and config.match("web/package-lock.json", skip))
        self.assertEqual(grade.runnable(["tests/a.rs", "tests/common/mod.rs", "a_test.go", "src/test/FooTest.java",
                                         "tests/conftest.py", "x.test.ts"]),
                         ["tests/a.rs", "a_test.go", "src/test/FooTest.java", "x.test.ts"])

    def test_command_placeholders(self):
        go = grade.command("go test -json {dirs} > {report}", ["pkg/a_test.go", "pkg/b_test.go", "c_test.go"], "r")
        self.assertEqual(go, "go test -json ./pkg . > r")
        self.assertEqual(grade.command("mvn test -Dtest={names:,}", ["src/test/a/FooTest.java", "src/test/BarTest.java"]),
                         "mvn test -Dtest=FooTest,BarTest")
        self.assertEqual(grade.command("cargo test --test {names: --test }", ["tests/a.rs", "tests/b.rs"]),
                         "cargo test --test a --test b")

    def test_stacks(self):
        cases = [({"uv.lock": ""}, "uv run pytest -q {files} --junitxml={report}"),
                 ({"package-lock.json": "", "package.json": '{"devDependencies": {"vitest": "1"}}'}, "npx vitest run"),
                 ({"pnpm-lock.yaml": "", "package.json": '{"scripts": {"test": "node --test"}}'}, "node --test"),
                 ({"yarn.lock": "", ".yarnrc.yml": "", "package.json": '{"jest": {}}'}, "yarn jest"),
                 ({"go.mod": ""}, "go test -json {dirs}"), ({"Cargo.toml": ""}, "cargo test"),
                 ({"pom.xml": ""}, "-Dtest={names:,}"), ({"build.gradle.kts": ""}, "test --tests")]
        for files, expected in cases:
            with tempfile.TemporaryDirectory() as d:
                for name, text in files.items():
                    Path(d, name).write_text(text)
                self.assertIn(expected, stacks.detect(Path(d))["test"], files)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "package-lock.json").write_text("")
            self.assertIsNone(stacks.detect(Path(d)))  # no known runner: ask instead of guessing

    def test_stats(self):
        self.assertAlmostEqual(stats.wilson(0, 10)[1], 0.2775, places=3)
        self.assertEqual(stats.wilson(10, 10)[1], 1.0)
        self.assertAlmostEqual(stats.sign_test(13, 1), 0.0018, places=4)
        self.assertAlmostEqual(stats.sign_test(5, 2), 0.453, places=3)
        self.assertEqual(stats.sign_test(0, 0), 1.0)
        lo, hi = stats.bootstrap([1, 1, 0, 0.5, 1, 0])
        self.assertTrue(0 <= lo < 0.58 < hi <= 1)
        self.assertLess(stats.bootstrap([1.0, 1.0])[0], 0.5)  # 2 tasks can't prove 100%

    def test_durations(self):
        self.assertEqual([config.secs(v) for v in ("90", "90s", "30m", "2h")], [90, 90, 1800, 7200])

    def test_parsers(self):
        claude = json.dumps({"type": "result", "is_error": False, "num_turns": 7, "total_cost_usd": 0.12,
                             "modelUsage": {"m-big": {"inputTokens": 10, "cacheReadInputTokens": 90,
                                                      "outputTokens": 5, "costUSD": 0.1},
                                            "m-small": {"inputTokens": 1, "outputTokens": 1, "costUSD": 0.02}}})
        self.assertEqual(agents.parse(CFG, "claude-json", "warning\n" + claude, []),
                         {"cost_usd": 0.12, "tokens_in": 101, "tokens_out": 6, "turns": 7, "model": "m-big",
                          "error": False})
        codex = "\n".join(json.dumps(e) for e in [
            {"type": "thread.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 500_000,
                                                 "output_tokens": 100_000}}])
        self.assertEqual(agents.parse(CFG, "codex-jsonl", codex, ["codex", "exec", "-m", "gpt-x"])["cost_usd"], 1.55)
        gemini = json.dumps({"response": "ok", "stats": {"models": {"gem-x": {"tokens": {
            "prompt": 1_000_000, "candidates": 50_000, "thoughts": 50_000, "cached": 0, "total": 1_100_000}}}}})
        self.assertEqual(agents.parse(CFG, "gemini-json", gemini, [])["cost_usd"], 4.0)
        aider = "Model: gpt-x with diff edit format\nTokens: 2.5k sent, 300 received. Cost: $0.01 message, $0.01 session.\n" \
                "Tokens: 1k sent, 1.2k cache write, 200 received. Cost: $0.02 message, $0.03 session.\n"
        self.assertEqual(agents.parse(CFG, "aider-text", aider, []),
                         {"cost_usd": 0.03, "tokens_in": 3500, "tokens_out": 500, "model": "gpt-x"})


class TestResults(unittest.TestCase):
    def parse(self, text, name="r.xml", roots=()):
        with tempfile.TemporaryDirectory() as d:
            Path(d, name).write_text(text, "utf-8")
            return testresults.parse(Path(d, name), "", roots)

    def test_junit(self):
        xml = ('<testsuites><testsuite name="pytest"><testcase classname="tests.test_a" name="test_ok"/>'
               '<testcase classname="tests.test_a" name="test_bad"><failure/></testcase>'
               '<testcase classname="tests.test_a" name="test_skip"><skipped/></testcase></testsuite></testsuites>')
        self.assertEqual(self.parse(xml), {"pytest::tests.test_a::test_ok": True, "pytest::tests.test_a::test_bad": False})
        node = ('<testsuite name="math"><testcase name="adds" classname="test" file="C:\\ws\\repo\\a.test.js"/>'
                '<testcase name="adds" classname="test" file="/wa/repo/b.test.js"><error/></testcase></testsuite>')
        self.assertEqual(self.parse(node, roots=["C:\\ws\\repo", "/wa/repo"]),
                         {"math::a.test.js::test::adds": True, "math::b.test.js::test::adds": False})

    def test_jest_go_cargo(self):
        jest = {"testResults": [{"name": "/wa/repo/src/a.test.js", "assertionResults": [
            {"fullName": "a works", "status": "passed"}, {"fullName": "a fails", "status": "failed"},
            {"fullName": "a later", "status": "pending"}]}, {"name": "/wa/repo/src/b.test.js", "status": "failed"}]}
        self.assertEqual(self.parse(json.dumps(jest), "r.json", ["/wa/repo"]),
                         {"src/a.test.js::a works": True, "src/a.test.js::a fails": False, "src/b.test.js::(file)": False})
        go = "\n".join(json.dumps(e) for e in [{"Action": "run", "Package": "m/p", "Test": "TestA"},
                                               {"Action": "pass", "Package": "m/p", "Test": "TestA"},
                                               {"Action": "fail", "Package": "m/p", "Test": "TestB"},
                                               {"Action": "fail", "Package": "m/p"}])
        self.assertEqual(self.parse(go, "r.json"), {"m/p::TestA": True, "m/p::TestB": False, "m/p::(package)": False})
        cargo = ("     Running tests/api.rs (target/debug/deps/api-1a2b)\n\nrunning 2 tests\n"
                 "test parses ... ok\ntest rejects ... FAILED\ntest slow ... ignored\n")
        self.assertEqual(testresults.parse(None, cargo), {"tests/api.rs::parses": True, "tests/api.rs::rejects": False})
        self.assertIsNone(testresults.parse(None, "....\nOK\n"))


class Reports(unittest.TestCase):
    TASKS = [{"id": "t1", "statement": "one"}, {"id": "t2", "statement": "two"}]

    def test_versions_are_never_silently_mixed(self):
        rs = [result("t1", "claude", "1.0", "solved"), result("t2", "claude", "1.1", "failed")]
        self.assertEqual([n for n, _ in report.analyze(self.TASKS, rs)[0]], ["claude"])
        self.assertIn("--by-version", report.terminal(self.TASKS, rs))
        self.assertEqual(sorted(n for n, _ in report.analyze(self.TASKS, rs, by_version=True)[0]),
                         ["claude 1.0", "claude 1.1"])
        retried = rs + [{**rs[0], "agent_version": "1.1", "status": "failed"}]
        self.assertEqual(len(store.latest(retried)), 2)
        self.assertEqual(len(store.latest(retried, by_version=True)), 3)

    def test_card_and_baseline(self):
        now = [result("t1", "claude", "1", "solved"), result("t2", "claude", "1", "solved")]
        then = [result("t1", "claude", "1", "failed"), result("t2", "claude", "1", "failed")]
        svg = report.card(self.TASKS, now, "repo")
        self.assertEqual(ET.fromstring(svg).tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("claude", svg)
        _, _, per = report.analyze(self.TASKS, now)
        self.assertEqual(report.versus(per, then), [["claude", "0% -> 100%", "2 up, 0 down", "no real change (p = 0.5)"]])


class Sandbox(unittest.TestCase):
    def test_docker_command(self):
        ws = Path(tempfile.gettempdir()) / "tmpx" / "repo"
        box = sandbox.Docker(ws, "img:1", ["~/.claude:/root/.claude:ro"])
        argv = box.argv("n1", ["ANTHROPIC_API_KEY"], stdin=True)
        self.assertEqual(argv[:5] + argv[-2:], ["docker", "run", "--rm", "--name", "n1", "-i", "img:1"])
        self.assertIn(f"{ws.parent}:/wa", argv)
        self.assertEqual(argv[argv.index("-e") + 1], "ANTHROPIC_API_KEY")
        self.assertEqual(box.path(ws.parent / "prompt.txt"), "/wa/prompt.txt")
        self.assertNotIn("PATH", box.env({"env": {}}))  # host paths mean nothing in the container

    def test_env(self):
        os.environ["WA_TOKEN"] = "s3cret"
        try:
            env = sandbox.Host(Path(".")).env({"env": {"TOKEN": "$WA_TOKEN", "MODE": 1}})
        finally:
            del os.environ["WA_TOKEN"]
        self.assertEqual((env["TOKEN"], env["MODE"]), ("s3cret", "1"))
        self.assertNotIn("WA_TOKEN", env)

    def test_jsonc(self):
        text = '{\n  // comment\n  "image": "a//b", /* block */ "x": [1, 2,],\n  "s": "q\\"//"\n}'
        self.assertEqual(json.loads(sandbox.jsonc(text)), {"image": "a//b", "x": [1, 2], "s": 'q"//'})


if __name__ == "__main__":
    unittest.main()
