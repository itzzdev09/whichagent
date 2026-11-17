"""Fake agents with known behavior, for testing whichagent without API spend (ARCHITECTURE.md section 17).

Usage: fake_agent.py perfect|noop|tamper|breaker|sleeper|peeker|escaper|hooker. They know the bugs in tests/repos.py.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BEAT = "import sys, time\nwhile True:\n    open(sys.argv[1], 'w').write(str(time.time()))\n    time.sleep(0.05)\n"


def edit(path, old, new):
    f = Path(path)
    f.write_text(f.read_text().replace(old, new))


def main(kind):
    sys.stdin.read()  # the prompt
    if kind in ("perfect", "breaker"):
        Path("tests/__pycache__").mkdir(exist_ok=True)  # what running the tests leaves behind
        Path("tests/__pycache__/test_calc.cpython-312.pyc").write_bytes(b"\0")
        edit("calc.py", "a // b", "a / b")
        if "def mod" not in Path("calc.py").read_text():
            with open("calc.py", "a") as f:
                f.write("\n\ndef mod(a, b):\n    return a % b\n")
    if kind == "breaker":
        edit("calc.py", "return a - b", "return a + b")
    if kind == "tamper":
        Path("tests/test_calc.py").write_text("import unittest\n")
    if kind == "sleeper":
        subprocess.Popen([sys.executable, "-c", BEAT, os.environ["WA_BEAT"]])
        time.sleep(120)
    if kind == "escaper":  # host mode can't stop this one (see ARCHITECTURE.md section 14); docker mode must
        target = Path(os.environ["WA_SENTINEL"])
        print(json.dumps({"read": target.read_text() if target.exists() else None}), file=sys.stderr)
    if kind == "hooker":  # docker mode: the host's `git add` must never run this
        subprocess.run(["git", "config", "core.fsmonitor", f"echo pwned > '{os.environ['WA_PWNED']}'; false"])
    if kind == "peeker":  # reports what it can see; the test decides whether anything leaked
        log = subprocess.run(["git", "log", "--all", "--format=%H"], capture_output=True, text=True).stdout.split()
        objects = subprocess.run(["git", "cat-file", "--batch-all-objects", "--batch"], capture_output=True).stdout
        found = [s for s in ("return a / b", "return a % b") if s.encode() in objects]
        print(json.dumps({"log": log, "found": found, "env": sorted(os.environ)}), file=sys.stderr)
    print(json.dumps({"type": "result", "is_error": False, "num_turns": 1, "total_cost_usd": 0.01,
                      "modelUsage": {"fake-1": {"inputTokens": 100, "outputTokens": 10, "costUSD": 0.01}}}))


if __name__ == "__main__":
    main(sys.argv[1])
