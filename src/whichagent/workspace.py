"""Ready-to-use workspaces: a leak-free clone at the base, dependencies (restored from the cache when the
lockfiles match), setup. Mining, running and doctor all use these, so tasks are validated where agents run."""
import hashlib
import os
import shutil
import tempfile
import threading
from pathlib import Path

from . import config as c, git as g, sandbox as sb, store

KEEP = 3  # dependency sets cached per repo, most recently used first; each can be hundreds of MB


def create(cfg, task_id, base):
    """A fresh clone of the task's base in the temp dir, and the executor that runs commands in it."""
    root = cfg["root"]
    bare = g.base_clone(root, base, store.state(root) / "bases" / f"{task_id}.git", cfg["mine"]["history_depth"])
    ws = g.workspace(bare, cfg["branch"], root.name)
    return ws, sb.executor(cfg, ws)


def prepare(cfg, ws, box, logs):
    """Install dependencies (or restore them from the cache), then run setup. False when a step fails.

    Output goes to install.log and setup.log in logs."""
    rp, timeout = cfg["repo"], c.secs(cfg["repo"]["setup_timeout"])
    if rp["install"]:
        key = _key(cfg, ws)
        if not (rp["cache"] and _restore(cfg, key, ws)):
            if box.shell(rp["install"], timeout, logs / "install.log")[0] != 0:
                return False
            if rp["cache"]:
                _save(cfg, key, ws)
    if rp["setup"] and box.shell(rp["setup"], timeout, logs / "setup.log")[0] != 0:
        return False
    g.exclude_untracked(ws)
    return True


def remove(ws, box):
    box.close()
    g.rmtree(ws.parent)


def cache_dir(cfg):
    """In the temp dir, like workspaces: a dependency cache in a synced folder would sync gigabytes."""
    root = cfg["root"]
    tag = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / "whichagent-cache" / f"{store.slug(root.name)}-{tag}"


def _key(cfg, ws):
    """The install command plus every lockfile's content: equal keys mean equal dependencies."""
    h = hashlib.sha256(cfg["repo"]["install"].encode())
    for p in sorted(g.git(ws, "ls-files").splitlines()):
        if c.match(p, cfg["repo"]["skip_if_changed"]):
            h.update(p.encode() + b"\0" + Path(ws, p).read_bytes())
    return h.hexdigest()[:16]


def _save(cfg, key, ws):
    """Cache everything install created (node_modules at every level, and so on)."""
    dest = cache_dir(cfg) / key
    if dest.exists():
        return
    tmp = dest.with_name(f"{key}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        tmp.mkdir(parents=True)
        for p in g.git(ws, "ls-files", "--others", "--directory").splitlines():
            src, to = ws / p.rstrip("/"), tmp / p.rstrip("/")
            to.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, to, symlinks=True)
            elif src.exists():
                shutil.copy2(src, to, follow_symlinks=False)
        tmp.rename(dest)
    except OSError:
        g.rmtree(tmp)  # another worker saved it first, or the disk is full: the cache is only a speedup
    old = sorted((p for p in dest.parent.iterdir() if ".tmp-" not in p.name), key=lambda p: p.stat().st_mtime)
    for p in old[:-KEEP]:
        g.rmtree(p)


def _restore(cfg, key, ws):
    src = cache_dir(cfg) / key
    if not src.is_dir():
        return False
    try:
        os.utime(src)  # most recently used
        shutil.copytree(src, ws, symlinks=True, dirs_exist_ok=True)
        return True
    except OSError:
        return False  # evicted meanwhile: install instead
