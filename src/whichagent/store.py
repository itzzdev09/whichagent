"""The .whichagent/ directory: tasks, results and run logs, kept as append-only JSONL so every command can resume."""
import json
import re
from pathlib import Path


def state(root):
    """.whichagent/ in the repo, created with a .gitignore that keeps it out of git."""
    d = Path(root) / ".whichagent"
    if not d.exists():
        d.mkdir()
        (d / ".gitignore").write_text("*\n")
    return d


def read(path):
    out = []
    if Path(path).exists():
        for line in Path(path).read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # a line cut short by a crash
    return out


def append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def latest(results, by_version=False):
    """The last result per run: a retried run replaces the earlier one. With by_version, runs of different
    agent versions are kept apart instead."""
    def key(r):
        return r["task"], r["agent"], r["attempt"], r.get("agent_version") if by_version else None
    return list({key(r): r for r in results}.values())


def slug(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "x"


def run_dir(root, task_id, agent, version, attempt):
    """Prompt, logs, diff and test output of one run."""
    return state(root) / "runs" / task_id / slug(f"{agent}@{version}" if version else agent) / str(attempt)
