"""whichagent.toml: defaults, stack detection, built-in agents, path patterns, state files."""
import functools
import json
import re
import shlex
import shutil
import tomllib
from pathlib import Path

from . import git

DEFAULTS = {
    "repo": {
        "setup": "",
        "test": "",
        "test_patterns": ["tests/**", "test/**", "**/__tests__/**", "test_*.py", "*_test.py", "*.test.*", "*.spec.*",
                          "*.test-*.*", "*.spec-*.*"],  # vitest type tests (x.test-d.ts) and the like
        "skip_if_changed": ["uv.lock", "poetry.lock", "Pipfile.lock", "requirements*.txt",
                            "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock*"],
        "setup_timeout": "10m",
        "test_timeout": "10m",
    },
    "mine": {"since": "12 months ago", "max_tasks": 25, "max_candidates": 300, "max_files": 5, "max_lines": 200,
             "verify_runs": 2, "min_statement_words": 8, "history_depth": 200},
    "run": {"agents": [], "attempts": 1, "jobs": 3, "jobs_per_agent": 0, "timeout": "30m",
            "max_cost": 25.0, "regression": True},
}
# Test config is reset like tests. Project manifests can hold legitimate changes, so they're only flagged.
TEST_CONFIG = ["conftest.py", "pytest.ini", "tox.ini", "jest.config.*", "jest.setup.*",
               "vitest.config.*", "vitest.setup.*", "vitest.workspace.*", ".mocharc*"]
FLAGGED = ["pyproject.toml", "setup.cfg", "package.json"]

BUILTIN = {
    "claude": {"cmd": "claude -p --output-format json --dangerously-skip-permissions", "parse": "claude-json"},
    # Unverified: flags and output formats come from each CLI's docs and haven't been run by whichagent yet.
    # `whichagent doctor` checks the flags against the installed CLI's --help.
    "codex": {"cmd": "codex exec --json --full-auto -", "parse": "codex-jsonl", "unverified": True},
    "gemini": {"cmd": "gemini --output-format json --yolo", "parse": "gemini-json", "unverified": True},
    "aider": {"cmd": "aider --message-file {prompt_file} --yes-always --no-auto-commits",
              "stdin": False, "parse": "aider-text", "unverified": True},
}
AGENT_KEYS = {"name", "extends", "cmd", "args", "stdin", "parse", "timeout", "env", "overlay"}

TEMPLATE = """# whichagent config: https://github.com/itzzdev09/whichagent/blob/main/ARCHITECTURE.md#12-configuration
[repo]
setup = {setup}
test  = {test}   # {{files}} = the task's test files

[run]
max_cost = 25.0   # USD, hard stop
"""


def load(root, overrides=None):
    """Config for the repo at root: defaults < whichagent.toml < overrides ({section: {key: value}})."""
    root = Path(root).resolve()
    f = root / "whichagent.toml"
    try:
        data = tomllib.loads(f.read_text("utf-8")) if f.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"whichagent.toml: {e}")
    bad = [f"[{s}]" for s in data if s not in (*DEFAULTS, "agent", "prices")]
    bad += [f"[{s}] {k}" for s in DEFAULTS for k in data.get(s, {}) if k not in DEFAULTS[s]]
    bad += [f"[[agent]] {k}" for a in data.get("agent", []) for k in a if k not in AGENT_KEYS]
    bad += ["[[agent]] without a name" for a in data.get("agent", []) if "name" not in a]
    if bad:
        raise SystemExit("whichagent.toml: unknown " + ", ".join(bad))
    cfg = {s: {**d, **data.get(s, {}), **(overrides or {}).get(s, {})} for s, d in DEFAULTS.items()}
    if not cfg["repo"]["test"] and (found := detect(root)):
        cfg["repo"]["setup"] = cfg["repo"]["setup"] or found[0]
        cfg["repo"]["test"] = found[1]
    if cfg["repo"]["test"] and "{files}" not in cfg["repo"]["test"]:
        raise SystemExit("The test command needs {files} where the test files go, e.g. pytest -q {files}")
    defined = {a["name"]: a for a in data.get("agent", [])}
    cfg["agents"] = {n: _agent(n, defined, cfg["run"]["timeout"]) for n in {**BUILTIN, **defined}}
    cfg.update(root=root, prices=data.get("prices", {}), branch=git.branch(root))
    return cfg


def _agent(name, defined, timeout, seen=()):
    """An agent with `extends` resolved: built-in < parent < its own keys."""
    if name in seen:
        raise SystemExit(f"agent {name!r}: extends loop")
    if name not in defined and name not in BUILTIN:
        raise SystemExit(f"unknown agent {name!r}")
    own = defined.get(name, {})
    ext = own.get("extends")
    base = _agent(ext, defined, timeout, seen + (name,)) if ext and ext != name else BUILTIN.get(name, {})
    a = {"args": "", "stdin": True, "parse": "none", "env": {}, "overlay": {}, "timeout": timeout,
         **base, **own, "name": name}
    if "cmd" not in a:
        raise SystemExit(f"agent {name!r}: needs cmd or extends")
    return a


def selected(cfg):
    """Agents to run: [run] agents, else your [[agent]] entries plus the built-ins found on PATH."""
    names = cfg["run"]["agents"] or [n for n, a in cfg["agents"].items()
                                     if n not in BUILTIN or shutil.which(argv(a)[0])]
    missing = [n for n in names if n not in cfg["agents"]]
    if missing:
        raise SystemExit(f"unknown agent: {', '.join(missing)}")
    return [cfg["agents"][n] for n in names]


def argv(agent):
    return shlex.split(agent["cmd"]) + shlex.split(agent["args"])


def detect(root):
    """(setup, test) commands for the repo's stack, or None."""
    if (root / "uv.lock").exists():
        return "uv sync", "uv run pytest -q {files}"
    if (root / "poetry.lock").exists():
        return "poetry install", "poetry run pytest -q {files}"
    pkg = (root / "package.json").read_text("utf-8") if (root / "package.json").exists() else ""
    runner = "vitest run" if '"vitest"' in pkg else "jest" if '"jest"' in pkg else None
    test = "node --test {files}" if "node --test" in pkg and not runner else None
    if (root / "package-lock.json").exists() and (runner or test):
        return "npm ci", test or f"npx {runner} {{files}}"
    if (root / "pnpm-lock.yaml").exists() and (runner or test):
        return "pnpm install --frozen-lockfile", test or f"pnpm exec {runner} {{files}}"
    return None


def write_template(root, setup, test):
    (root / "whichagent.toml").write_text(TEMPLATE.format(setup=json.dumps(setup), test=json.dumps(test)), "utf-8")


@functools.lru_cache(maxsize=None)
def _rx(pattern):
    tokens = {"**/": "(?:.*/)?", "/**": "/.*", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    return re.compile("".join(tokens.get(t, re.escape(t)) for t in re.split(r"(\*\*/|/\*\*$|\*\*|\*|\?)", pattern)))


def match(path, patterns):
    """gitignore-style glob: a pattern without '/' matches the file name at any depth."""
    name = path.rsplit("/", 1)[-1]
    return any(_rx(p).fullmatch(path if "/" in p else name) for p in patterns)


def is_test(cfg, path):
    return match(path, cfg["repo"]["test_patterns"]) or match(path, TEST_CONFIG)


def secs(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", str(value).strip())
    if not m:
        raise SystemExit(f"bad duration {value!r}: use e.g. 90s, 30m or 2h")
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[m[2]]


def state(root):
    """The .whichagent/ dir, created with a .gitignore that keeps it out of git."""
    d = Path(root) / ".whichagent"
    if not d.exists():
        d.mkdir()
        (d / ".gitignore").write_text("*\n")
    return d


def read_jsonl(path):
    out = []
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # a line cut short by a crash
    return out


def latest(results):
    """The last result per (task, agent, attempt): a retried run replaces the earlier one."""
    return list({(r["task"], r["agent"], r["attempt"]): r for r in results}.values())


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def ask(question):
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False
