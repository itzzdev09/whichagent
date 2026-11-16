"""git: history, leak-free base clones, workspaces, diffs."""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# Untracked files an agent leaves by running tools: not part of its change. Tracked files are unaffected.
EXCLUDE = """.aider*
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
node_modules/
"""
_refs = threading.Lock()


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.strip()[-500:]}")
    return r.stdout


def show(repo, sha, path):
    """File content at a commit, or None if it doesn't exist there."""
    r = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def branch(repo):
    return git(repo, "branch", "--show-current").strip() or "main"


def files(text):
    """{path: [status, added, deleted]} from --raw --numstat output."""
    out = {}
    for line in text.splitlines():
        if line.startswith(":"):
            meta, path = line.split("\t", 1)
            out.setdefault(path, ["M", 0, 0])[0] = meta.split()[-1][0]
        elif m := re.fullmatch(r"(\d+|-)\t(\d+|-)\t(.+)", line):
            f = out.setdefault(m[3], ["M", 0, 0])
            f[1], f[2] = (int(x) if x != "-" else 0 for x in m.group(1, 2))
    return out


def commits(repo, since):
    """Non-merge commits on the current branch, newest first."""
    out = git(repo, "log", "HEAD", "--no-merges", "--no-renames", f"--since={since}",
              "--format=%x1e%H%x1f%P%x1f%cs%x1f%B%x1f", "--raw", "--numstat")
    for chunk in out.split("\x1e")[1:]:
        sha, parents, date, msg, rest = chunk.split("\x1f")
        yield {"sha": sha, "parents": parents.split(), "date": date, "msg": msg.strip(), "files": files(rest)}


def base_clone(repo, sha, dest, depth):
    """Bare shallow clone that ends at sha: the fix and every later commit never enter it."""
    tmp = f"whichagent/tmp-{sha[:12]}"
    with _refs:  # ref updates in your repo, one at a time
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "branch", "-f", tmp, sha)
        try:
            git(repo, "clone", "-q", "--bare", "--single-branch", "--no-tags", f"--depth={depth}",
                "--branch", tmp, Path(repo).as_uri(), str(dest))
        finally:
            git(repo, "branch", "-D", tmp)
    return dest


def workspace(bare, branch_name, name):
    """Fresh clone of a base in the system temp dir, on your default branch, with no remote."""
    ws = Path(tempfile.mkdtemp()) / name
    git(ws.parent, "clone", "-q", str(bare), str(ws))
    if git(ws, "branch", "--show-current").strip() != branch_name:
        git(ws, "branch", "-m", branch_name)
    git(ws, "remote", "remove", "origin")
    rmtree(ws / ".git" / "logs")  # the reflog names the base clone's path
    (ws / ".git" / "info").mkdir(exist_ok=True)
    (ws / ".git" / "info" / "exclude").write_text(EXCLUDE)
    return ws


def exclude_untracked(ws):
    """Keep what setup created (venvs, build output) out of the agent's diff."""
    new = git(ws, "ls-files", "--others", "--exclude-standard", "--directory").splitlines()
    with open(ws / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
        f.writelines(f"/{p}\n" for p in new)


def snapshot(ws):
    """Tree id of the workspace as the agent gets it, left unstaged."""
    git(ws, "add", "-A")
    tree = git(ws, "write-tree").strip()
    git(ws, "reset", "-q")
    return tree


def changes(ws, tree):
    """Stage everything; {path: [status, added, deleted]} against tree."""
    git(ws, "add", "-A")
    return files(git(ws, "diff", "--cached", "--no-renames", "--raw", "--numstat", tree))


def diff(ws, tree):
    return git(ws, "diff", "--cached", "--no-renames", "--no-color", "--no-ext-diff", tree)


def put(repo, sha, paths, ws):
    """Make paths in ws match commit sha of repo, deleting the ones absent there."""
    for p in paths:
        data, f = show(repo, sha, p), Path(ws, p)
        if data is None:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)


def rmtree(path):
    def retry(func, p, _):
        os.chmod(p, stat.S_IWRITE)  # git marks objects read-only
        func(p)
    try:
        shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))
    except OSError:
        pass  # ponytail: a file still held open (Windows) leaves the dir to the OS temp cleanup
