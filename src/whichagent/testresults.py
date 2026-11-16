"""Per-test results from test runners: JUnit XML (pytest, vitest, node, surefire, gradle), Jest JSON,
go test -json, and cargo test's own output."""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def parse(report, log="", roots=()):
    """{test id: passed} from report (a file, or a dir of XML files), else from the log; None when neither has any.

    Skipped tests are left out. Paths under roots are made relative, so ids match across workspaces."""
    out = {}
    p = Path(report) if report else None
    files = sorted(p.rglob("*.xml")) if p and p.is_dir() else [p] if p and p.is_file() else []
    for f in files:
        _merge(out, _report(f.read_text("utf-8", "replace")), roots)
    if not files:
        _merge(out, _cargo(log), roots)
    return out or None


def _merge(out, results, roots):
    for key, ok in results.items():
        for root in roots:
            key = key.replace(root + "/", "").replace(root + "\\", "")
        out[key] = out.get(key, True) and ok  # a test reported twice passes only if it passed both times


def _report(text):
    text = text.lstrip()
    if text.startswith("<"):
        return _junit(text)
    try:
        data = json.loads(text)
    except ValueError:
        return _go(text)
    return _jest(data) if isinstance(data, dict) and "testResults" in data else _go(text)


def _junit(text):
    out = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out

    def walk(nodes, suites):
        for n in nodes:
            if n.tag in ("testsuites", "testsuite"):
                walk(n, suites + [n.get("name", "")] if n.tag == "testsuite" else suites)
            elif n.tag == "testcase" and n.find("skipped") is None:
                key = "::".join(x for x in [*suites, n.get("file", ""), n.get("classname", ""), n.get("name", "")] if x)
                out[key] = out.get(key, True) and n.find("failure") is None and n.find("error") is None
    walk([root], [])
    return out


def _jest(data):
    out = {}
    for f in data.get("testResults") or []:
        cases = f.get("assertionResults") or []
        for a in cases:
            if a.get("status") in ("passed", "failed"):
                key = f"{f.get('name', '')}::{a.get('fullName') or a.get('title')}"
                out[key] = out.get(key, True) and a["status"] == "passed"
        if not cases and f.get("status") == "failed":
            out[f"{f.get('name', '')}::(file)"] = False  # the file didn't even load
    return out


def _go(text):
    out = {}
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("Action") in ("pass", "fail"):
            out[f"{e.get('Package', '')}::{e.get('Test') or '(package)'}"] = e["Action"] == "pass"
    return out


def _cargo(log):
    out, binary = {}, ""
    for line in log.splitlines():
        if m := re.match(r"\s*Running (.+?)(?: \(|$)", line):
            binary = m[1]
        elif m := re.match(r"test (\S+) \.\.\. (ok|FAILED)", line):
            out[f"{binary}::{m[1]}"] = m[2] == "ok"
    return out
