"""Running commands: as a process tree on this machine, or in a Docker container that sees only the workspace.

Both executors have the same methods (shell, agent, env, which, path, close), so mining, running and grading
don't care where commands run.
"""
import functools
import hashlib
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

STOP = threading.Event()   # set on Ctrl-C: no new process starts
LIVE = set()               # running processes, killed on Ctrl-C
CONTAINERS = set()         # running containers, killed on Ctrl-C
_ids = itertools.count()

# Credentials and proxies: the only host variables a container gets. Everything else comes from its image.
CONTAINER_KEEP = set("""ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION OPENAI_API_KEY
OPENAI_BASE_URL GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_LOCATION GOOGLE_GENAI_USE_VERTEXAI
OPENROUTER_API_KEY DEEPSEEK_API_KEY MISTRAL_API_KEY GROQ_API_KEY XAI_API_KEY AZURE_OPENAI_API_KEY DASHSCOPE_API_KEY
HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY""".split())
# On the host, agents also get what programs need to start: paths, home, temp, locale, the OS variables
# Windows needs, and the agents' config dirs. Nothing else from your shell.
ENV_KEEP = CONTAINER_KEEP | set("""PATH PATHEXT HOME USER LOGNAME SHELL TERM LANG LANGUAGE TZ TMPDIR TMP TEMP
USERPROFILE HOMEDRIVE HOMEPATH APPDATA LOCALAPPDATA PROGRAMDATA ALLUSERSPROFILE PUBLIC SYSTEMROOT SYSTEMDRIVE WINDIR
COMSPEC OS USERNAME USERDOMAIN COMPUTERNAME NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE PROGRAMFILES
PROGRAMFILES(X86) PROGRAMW6432 COMMONPROGRAMFILES COMMONPROGRAMFILES(X86) COMMONPROGRAMW6432 XDG_CONFIG_HOME
XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME XDG_RUNTIME_DIR SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS
REQUESTS_CA_BUNDLE CLAUDE_CONFIG_DIR CLAUDE_CODE_GIT_BASH_PATH CODEX_HOME GOOGLE_APPLICATION_CREDENTIALS""".split())


def proc(cmd, cwd, timeout, out, err=None, env=None, stdin=None, shell=False, container=None):
    """Run cmd with output to files, killing its process tree (and container) at timeout.

    Returns (exit code, or None on timeout; seconds)."""
    if STOP.is_set():
        raise RuntimeError("stopped")
    start = time.monotonic()
    with open(out, "wb") as o, open(err or os.devnull, "wb") as e, open(stdin or os.devnull, "rb") as i:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, shell=shell, stdin=i, stdout=o,
                             stderr=e if err else subprocess.STDOUT, start_new_session=os.name != "nt")
        LIVE.add(p)
        if container:
            CONTAINERS.add(container)
        try:
            if STOP.is_set():
                _stop(p, container)
            rc = p.wait(timeout)
        except subprocess.TimeoutExpired:
            rc = None
        # ponytail: on Windows, children an agent leaves behind after a normal exit survive; Job Objects would fix it
        if rc is None:
            _stop(p, container)
        elif os.name != "nt":
            kill(p)  # also stops what it left running in its process group
        LIVE.discard(p)
        CONTAINERS.discard(container)
    return rc, time.monotonic() - start


def _stop(p, container):
    if container:  # killing the docker CLI alone leaves the container running
        subprocess.run(["docker", "kill", container], capture_output=True)
    kill(p)


def kill(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    p.wait()


def killall():
    for name in CONTAINERS.copy():
        subprocess.run(["docker", "kill", name], capture_output=True)
    for p in LIVE.copy():
        kill(p)


def _own(agent):
    """The agent's own env. A value like "$NAME" is read from your environment, so secrets stay out of the TOML."""
    return {k: os.environ.get(v[1:], "") if isinstance(v, str) and v.startswith("$") else str(v)
            for k, v in agent["env"].items()}


class Host:
    """Runs commands on this machine, as your user. Not a sandbox: see ARCHITECTURE.md section 14."""

    def __init__(self, ws):
        self.ws = Path(ws)
        self.roots = [str(self.ws), self.ws.as_posix()]  # stripped from test ids, so they match across workspaces

    def path(self, p):
        return str(p)

    def which(self, binary):
        return shutil.which(binary)

    def env(self, agent):
        keep = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP or k.upper().startswith("LC_")}
        return {**keep, **_own(agent)}

    def shell(self, cmd, timeout, log):
        return proc(cmd, self.ws, timeout, log, shell=True)

    def agent(self, argv, timeout, out, err, env, stdin):
        return proc(argv, self.ws, timeout, out, err=err, env=env, stdin=stdin)

    def close(self):
        pass


class Docker:
    """Runs commands in a container that sees only this run's temp dir: the workspace, the prompt, the reports."""

    def __init__(self, ws, image, mounts=()):
        self.ws, self.image = Path(ws), image
        self.mounts = [os.path.expanduser(m) for m in mounts]
        self.cwd = f"/wa/{self.ws.name}"
        self.roots = [self.cwd]
        # From the agent's start on, .git is read-only in the container: git runs on the host, so a .git/config
        # the agent wrote (core.fsmonitor, filter drivers) would run its commands there.
        self.git_ro = False

    def path(self, p):
        return "/wa/" + Path(p).relative_to(self.ws.parent).as_posix()

    def which(self, binary):
        return binary  # it lives in the image; a missing one fails the run as an agent error

    def env(self, agent):
        return {**{k: v for k, v in os.environ.items() if k.upper() in CONTAINER_KEEP}, **_own(agent)}

    def argv(self, name, env=(), stdin=False):
        a = ["docker", "run", "--rm", "--name", name, "-v", f"{self.ws.parent}:/wa", "-w", self.cwd,
             # the container runs as root and the mount is owned by you: without this, git refuses the workspace
             "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory", "-e", "GIT_CONFIG_VALUE_0=*"]
        if self.git_ro:
            a += ["-v", f"{self.ws / '.git'}:{self.cwd}/.git:ro"]
        for m in self.mounts:
            a += ["-v", m]
        for k in env:  # by name only: values travel in the docker CLI's environment, not on its command line
            a += ["-e", k]
        return a + (["-i"] if stdin else []) + [self.image]

    def shell(self, cmd, timeout, log):
        name = _name()
        return proc(self.argv(name) + ["sh", "-c", cmd], self.ws, timeout, log, container=name)

    def agent(self, argv, timeout, out, err, env, stdin):
        name = _name()
        self.git_ro = True  # also for the tests after it, which run the agent's code
        return proc(self.argv(name, sorted(env), stdin is not None) + list(argv), self.ws, timeout, out, err=err,
                    env={**os.environ, **env}, stdin=stdin, container=name)

    def close(self):
        if hasattr(os, "getuid"):  # files the container wrote as root go back to you, so the run dir can be deleted
            subprocess.run(["docker", "run", "--rm", "-v", f"{self.ws.parent}:/wa", self.image, "chown", "-R",
                            f"{os.getuid()}:{os.getgid()}", "/wa"], capture_output=True)


def _name():
    return f"whichagent-{os.getpid()}-{next(_ids)}"


def executor(cfg, ws):
    if cfg["run"]["sandbox"] == "docker":
        return Docker(ws, image(cfg["root"], cfg["docker"]["image"]), cfg["docker"]["mounts"])
    return Host(ws)


def version(cfg, binary):
    """First line of `<binary> --version`, from the image in docker mode. Recorded on every result."""
    if cfg["run"]["sandbox"] == "docker":
        return _version(("docker", "run", "--rm", image(cfg["root"], cfg["docker"]["image"]), binary, "--version"))
    exe = shutil.which(binary)
    return _version((exe, "--version")) if exe else None


@functools.cache
def _version(argv):
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (r.stdout or r.stderr).strip().splitlines()
    return lines[0] if lines else None


@functools.cache
def image(root, configured=""):
    """The docker-mode image: [docker] image, else one built from .devcontainer/devcontainer.json or a Dockerfile."""
    if configured:
        return configured
    dockerfile = context = None
    for spec_path in (root / ".devcontainer" / "devcontainer.json", root / ".devcontainer.json"):
        if spec_path.exists():
            spec = json.loads(jsonc(spec_path.read_text("utf-8")))
            if spec.get("image"):
                return spec["image"]
            build = spec.get("build") or {}
            name = build.get("dockerfile") or spec.get("dockerFile")
            if not name:
                raise SystemExit(f"{spec_path}: docker mode needs an image or a dockerfile (compose isn't supported)")
            dockerfile, context = spec_path.parent / name, spec_path.parent / build.get("context", ".")
            break
    if dockerfile is None and (root / "Dockerfile").exists():
        dockerfile, context = root / "Dockerfile", root
    if dockerfile is None:
        raise SystemExit("Docker mode needs an image: set [docker] image, or add .devcontainer/devcontainer.json"
                         " or a Dockerfile")
    tag = "whichagent-" + hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode:
        if subprocess.run(["docker", "build", "-t", tag, "-f", str(dockerfile), str(context)]).returncode:
            raise SystemExit(f"docker build of {dockerfile} failed")
    return tag


def jsonc(text):
    """JSON with comments and trailing commas (devcontainer.json) as plain JSON."""
    out, i, string = [], 0, False
    while i < len(text):
        ch = text[i]
        if string:
            out.append(text[i:i + 2] if ch == "\\" else ch)
            i += 2 if ch == "\\" else 1
            string = ch != '"'
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        string = ch == '"'
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))
