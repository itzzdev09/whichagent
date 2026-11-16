import json
import unittest
from pathlib import Path

import tests  # noqa: F401  (puts src/ on sys.path)
from whichagent import agents, config, stats

CFG = {"root": Path("."), "prices": {"gpt-x": {"input": 1.0, "output": 10.0, "cache_read": 0.1},
                                     "gem-x": {"input": 2.0, "output": 20.0}}}


class Units(unittest.TestCase):
    def test_patterns(self):
        cfg = {"repo": config.DEFAULTS["repo"]}
        for p in ["tests/test_calc.py", "pkg/test_x.py", "src/a.test.ts", "src/__tests__/a.js", "conftest.py",
                  "src/a.test-d.ts"]:
            self.assertTrue(config.is_test(cfg, p), p)
        for p in ["calc.py", "src/testing.py", "docs/tests.md"]:
            self.assertFalse(config.is_test(cfg, p), p)
        skip = config.DEFAULTS["repo"]["skip_if_changed"]
        self.assertTrue(config.match("requirements-dev.txt", skip) and config.match("web/package-lock.json", skip))

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


if __name__ == "__main__":
    unittest.main()
