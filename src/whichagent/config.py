"""whichagent.toml: defaults, built-in agents, path patterns."""
import functools
import json
import re
import shlex
import shutil
import tomllib
from pathlib import Path

from . import git, stacks

DEFAULTS = {
    "repo": {
        "install": "",   # installs dependencies; with cache = true, cached by lockfile hash
        "setup": "",     # runs after install in every workspace: builds, codegen
        "test": "",      # runs the task's tests: {files}, {dirs} or {names}; {report} for per-test results
        "suite": "",     # runs the whole suite; default: test without the file placeholders
        "report": "",    # where the runner writes per-test results when test can't take {report}
        "cache": False,
        "test_patterns": ["tests/**", "test/**", "**/__tests__/**", "**/src/test/**", "**/testdata/**",
                          "test_*.py", "*_test.py", "*_test.go", "*.test.*", "*.spec.*",
                          "*.test-*.*", "*.spec-*.*"],  # the last two: vitest type tests (x.test-d.ts) and such
        "skip_if_changed": ["uv.lock", "poetry.lock", "Pipfile.lock", "requirements*.txt", "package-lock.json",
                            "pnpm-lock.yaml", "yarn.lock", "bun.lock*", "go.sum", "Cargo.lock", "gradle.lockfile"],
        "setup_timeout": "10m",
        "test_timeout": "10m",
    },
    "mine": {"since": "12 months ago", "max_tasks": 25, "max_candidates": 300, "max_files": 5, "max_lines": 200,
             "verify_runs": 2, "min_statement_words": 8, "history_depth": 200},
    "run": {"agents": [], "attempts": 1, "jobs": 3, "jobs_per_agent": 0, "timeout": "30m",
            "max_cost": 25.0, "regression": True, "sandbox": "host"},
    "docker": {"image": "", "mounts": []},
}
PLACEHOLDER = re.compile(r"\{(files|dirs|names)(?::([^}]*))?\}")  # {names:,} joins with "," instead of " "
# Test config is reset like tests. Project manifests can hold legitimate changes, so they're only flagged.
TEST_CONFIG = ["conftest.py", "pytest.ini", "tox.ini", "jest.config.*", "jest.setup.*",
               "vitest.config.*", "vitest.setup.*", "vitest.workspace.*", ".mocharc*"]
FLAGGED = ["pyproject.toml", "setup.cfg", "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle*"]

BUILTIN = {
    "claude": {"cmd": "claude -p --output-format json --dangerously-skip-permissions", "parse": "claude-json"},
    # Unverified: flags and output formats come from each CLI's docs and haven't been run by whichagent yet.
    # `whichagent doctor` checks the flags against the installed CLI's --help.
    "codex": {"cmd": "codex exec --json --sandbox workspace-write -", "parse": "codex-jsonl"},
    "gemini": {"cmd": "gemini --output-format json --yolo", "parse": "gemini-json", "unverified": True},
    "aider": {"cmd": "aider --message-file {prompt_file} --yes-always --no-auto-commits --no-gitignore"
                     " --no-check-update --no-show-release-notes --analytics-disable --no-pretty",
              "stdin": False, "parse": "aider-text"},
    "qwen": {"cmd": "qwen --output-format json --yolo", "parse": "gemini-json", "unverified": True},
    "opencode": {"cmd": "opencode run --auto --format json", "parse": "opencode-json"},
    "goose": {"cmd": "goose run --no-session --instructions {prompt_file}", "stdin": False, "unverified": True},
    "amp": {"cmd": "amp --execute --dangerously-allow-all", "unverified": True},
    "copilot": {"cmd": "copilot --allow-all-tools", "unverified": True},
    "cursor": {"cmd": "cursor-agent --print --force --output-format json", "unverified": True},
}
AGENT_KEYS = {"name", "extends", "cmd", "args", "stdin", "parse", "timeout", "env", "overlay"}


def load(root, overrides=None):
    """Config for the repo at root: defaults < detected stack < whichagent.toml < overrides ({section: {key: v}})."""
    root = Path(root).resolve()
    f = root / "whichagent.toml"
    try:
        data = tomllib.loads(f.read_text("utf-8")) if f.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"whichagent.toml: {e}")
    overrides = overrides or {}
    bad = [f"[{s}]" for s in data if s not in (*DEFAULTS, "agent", "prices")]
    bad += [f"[{s}] {k}" for s in DEFAULTS for k in data.get(s, {}) if k not in DEFAULTS[s]]
    bad += [f"[[agent]] {k}" for a in data.get("agent", []) for k in a if k not in AGENT_KEYS]
    bad += ["[[agent]] without a name" for a in data.get("agent", []) if "name" not in a]
    if bad:
        raise SystemExit("whichagent.toml: unknown " + ", ".join(bad))
    cfg = {s: {**d, **data.get(s, {}), **overrides.get(s, {})} for s, d in DEFAULTS.items()}
    given = {**data.get("repo", {}), **overrides.get("repo", {})}
    if not cfg["repo"]["test"] and (found := stacks.detect(root)):
        cfg["repo"].update({k: v for k, v in found.items() if k not in given})
    if cfg["repo"]["test"] and not PLACEHOLDER.search(cfg["repo"]["test"]):
        raise SystemExit("The test command needs {files} (or {dirs} or {names}) where the task's tests go,"
                         " e.g. pytest -q {files}")
    if cfg["run"]["sandbox"] not in ("host", "docker"):
        raise SystemExit("[run] sandbox is host or docker")
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
    names = cfg["run"]["agents"]
    if not names and cfg["run"]["sandbox"] == "docker":
        raise SystemExit("Docker mode: list the agents installed in the image in [run] agents or --agents")
    names = names or [n for n, a in cfg["agents"].items() if n not in BUILTIN or shutil.which(argv(a)[0])]
    missing = [n for n in names if n not in cfg["agents"]]
    if missing:
        raise SystemExit(f"unknown agent: {', '.join(missing)}")
    return [cfg["agents"][n] for n in names]


def argv(agent):
    return shlex.split(agent["cmd"]) + shlex.split(agent["args"])


def write_template(root, repo):
    lines = ["# whichagent config: https://github.com/itzzdev09/whichagent/blob/main/ARCHITECTURE.md#12-configuration",
             "[repo]"]
    lines += [f"{k} = {json.dumps(repo[k])}" for k in ("install", "setup", "test", "suite", "report", "cache")
              if repo.get(k)]
    lines += ["", "[run]", "max_cost = 25.0   # USD, hard stop", ""]
    (root / "whichagent.toml").write_text("\n".join(lines), "utf-8")


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


def ask(question):
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False
