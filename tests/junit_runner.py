"""Runs unittest test files and writes JUnit XML, so the fixture repos get per-test results with the stdlib only.

Usage: junit_runner.py REPORT [FILE...]   (no files: discover every test)
"""
import os
import sys
import unittest
from xml.sax.saxutils import quoteattr


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append((test.id(), ""))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cases.append((test.id(), "failure"))

    def addError(self, test, err):
        super().addError(test, err)
        self.cases.append((test.id(), "error"))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases.append((test.id(), "skipped"))


def main(report, files):
    sys.path[0] = os.getcwd()  # import the repo under test, not this script's directory
    loader = unittest.TestLoader()
    suite = (unittest.TestSuite(loader.loadTestsFromName(f[:-3].replace("/", ".")) for f in files)
             if files else loader.discover("."))
    result = unittest.TextTestRunner(resultclass=Result, verbosity=0).run(suite)
    with open(report, "w", encoding="utf-8") as f:
        f.write("<testsuites><testsuite name='unittest'>")
        for test_id, kind in result.cases:
            cls, _, name = test_id.rpartition(".")
            f.write(f"<testcase classname={quoteattr(cls)} name={quoteattr(name)}>"
                    + (f"<{kind}/>" if kind else "") + "</testcase>")
        f.write("</testsuite></testsuites>")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
