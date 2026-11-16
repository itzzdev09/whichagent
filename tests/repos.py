"""Builds tiny git repos with known fix commits, for testing whichagent itself.

Run `python tests/repos.py <dir>` to build the calculator repo by hand.
"""
import os
import subprocess
import sys
from pathlib import Path

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

CALC = """def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def div(a, b):
    return a // b
"""

TEST_CALC = """import unittest
from calc import add


class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
"""

TEST_SUB = """import unittest
from calc import sub


class T(unittest.TestCase):
    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
"""

TEST_MOD = """import unittest
from calc import mod


class T(unittest.TestCase):
    def test_mod(self):
        self.assertEqual(mod(7, 3), 1)
"""

# Passes on the 2nd run, fails on the 4th: validation sees a fail/pass pair, then a disagreement.
TEST_FLAKY = """import os
import unittest
import calc


class T(unittest.TestCase):
    def test_neg(self):
        n = int(open(".count").read()) + 1 if os.path.exists(".count") else 1
        open(".count", "w").write(str(n))
        self.assertTrue(hasattr(calc, "neg") and n < 4)
"""


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, msg, files):
    for path, text in files.items():
        f = Path(repo, path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def calc_repo(path):
    """A calculator repo. Returns {name: sha}. Mining should find exactly `div` (hidden) and `mod` (visible)."""
    path = Path(path)
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    calc = CALC
    s = {"init": commit(path, "Initial calculator with tests",
                        {"calc.py": calc, "README.md": "# calc\n", "tests/__init__.py": "",
                         "tests/test_calc.py": TEST_CALC, "tests/test_sub.py": TEST_SUB})}
    calc = calc.replace("a // b", "a / b")
    s["div"] = commit(path, "Fix div so it returns true division instead of flooring the result",
                      {"calc.py": calc, "tests/test_calc.py": TEST_CALC.replace("import add", "import add, div")
                       + "\n    def test_div(self):\n        self.assertEqual(div(7, 2), 3.5)\n"})
    s["docs"] = commit(path, "Document every function of the calculator in the README file",
                       {"README.md": "# calc\n\nadd, sub, div\n"})
    calc += "\n\ndef mod(a, b):\n    return a % b\n"
    s["mod"] = commit(path, "Add a mod helper that returns the remainder of a division",
                      {"calc.py": calc, "tests/test_mod.py": TEST_MOD})
    calc += "\n# handles negative numbers\n"
    s["deps"] = commit(path, "Pin requirements and make sure sub handles negative numbers",
                       {"calc.py": calc, "requirements.txt": "six==1.16.0\n",
                        "tests/test_sub.py": TEST_SUB + "\n    def test_neg(self):\n        self.assertEqual(sub(-1, 1), -2)\n"})
    calc += "# fast path\n"
    s["short"] = commit(path, "fix mul", {"calc.py": calc, "tests/test_calc.py": TEST_CALC + "\n# mul\n"})
    calc += "# adds negatives too\n"
    s["passes"] = commit(path, "Add a test for adding negative numbers, which already worked fine",
                         {"calc.py": calc, "tests/test_calc.py": TEST_CALC.replace("import add", "import add, div")
                          + "\n    def test_div(self):\n        self.assertEqual(div(7, 2), 3.5)\n"
                          + "\n    def test_neg(self):\n        self.assertEqual(add(-2, -3), -5)\n"})
    calc += "\n\ndef neg(a):\n    return -a\n"
    s["flaky"] = commit(path, "Add a neg helper that flips the sign of a number for callers",
                        {"calc.py": calc, "tests/test_flaky.py": TEST_FLAKY})
    return s


if __name__ == "__main__":
    calc_repo(sys.argv[1])
    print(f"built {sys.argv[1]}")
