"""Built-in adapters' output parsers, model prices and flag checks."""
import functools
import itertools
import json
import os
import re
import subprocess
import threading
import time
import tomllib
import urllib.request
from pathlib import Path

PRICES_URL = "https://models.dev/api.json"


def parse(cfg, kind, out, argv):
    """Usage from an agent's stdout: cost_usd, tokens_in, tokens_out, turns, model, error (all optional)."""
    return PARSERS[kind](cfg, out, argv)


def _json(out):
    """The output as JSON: the whole text, else its last line that parses."""
    for text in [out, *reversed(out.strip().splitlines())]:
        try:
            return json.loads(text)
        except ValueError:
            pass
    return None


def claude(cfg, out, argv):
    d = _json(out)
    if isinstance(d, list):
        d = next((x for x in reversed(d) if isinstance(x, dict) and x.get("type") == "result"), None)
    if not isinstance(d, dict):
        return {"error": True}
    mu = d.get("modelUsage") or {}
    u = d.get("usage") or {}
    if mu:
        tin = sum(m.get("inputTokens", 0) + m.get("cacheReadInputTokens", 0) + m.get("cacheCreationInputTokens", 0)
                  for m in mu.values())
        tout = sum(m.get("outputTokens", 0) for m in mu.values())
    else:
        tin = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        tout = u.get("output_tokens", 0)
    usd = d.get("total_cost_usd")
    return {"cost_usd": None if usd is None else round(usd, 6), "tokens_in": tin, "tokens_out": tout,
            "turns": d.get("num_turns"),
            "model": max(mu, key=lambda k: mu[k].get("costUSD", 0), default=None), "error": bool(d.get("is_error"))}


def _events(out):
    """The JSON objects in JSONL output, skipping anything else."""
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            yield e


def codex(cfg, out, argv):
    """`codex exec --json` events, token usage on turn.completed."""
    tin = tout = cached = 0
    error = False
    for e in _events(out):
        if e.get("type") == "turn.completed":
            u = e.get("usage") or {}
            tin += u.get("input_tokens", 0)
            cached += u.get("cached_input_tokens", 0)
            tout += u.get("output_tokens", 0)
        error = error or e.get("type") in ("error", "turn.failed")
    model = _flag(argv, "-m", "--model") or _codex_model()
    return {"cost_usd": cost(cfg, model, tin, tout, cached), "tokens_in": tin, "tokens_out": tout,
            "model": model, "error": error}


def gemini(cfg, out, argv):
    """`gemini --output-format json`, tokens per model under stats.models. Qwen Code prints a list of
    events with the same stats on its "result" event."""
    d = _json(out)
    if isinstance(d, list):
        d = next((x for x in reversed(d) if isinstance(x, dict) and x.get("type") == "result"), None)
    if not isinstance(d, dict):
        return {"error": True}
    models = (d.get("stats") or {}).get("models") or {}
    tin = tout = 0
    total = 0.0 if models else None
    for name, m in models.items():
        t = m.get("tokens") or {}
        i, o = t.get("prompt", 0), t.get("candidates", 0) + t.get("thoughts", 0)
        tin, tout = tin + i, tout + o
        p = cost(cfg, name, i, o, t.get("cached", 0))
        total = None if p is None or total is None else total + p
    model = max(models, key=lambda n: (models[n].get("tokens") or {}).get("total", 0), default=None)
    return {"cost_usd": total, "tokens_in": tin, "tokens_out": tout, "model": model, "turns": d.get("num_turns"),
            "error": bool(d.get("error") or d.get("is_error"))}


def aider(cfg, out, argv):
    """aider's "Tokens: ... sent, ... received. Cost: ... $X session." lines; local models print no Cost."""
    def n(s):
        return float(s[:-1]) * {"k": 1e3, "M": 1e6}[s[-1]] if s[-1] in "kM" else float(s)
    toks = re.findall(r"Tokens: ([\d.]+[kM]?) sent.*?([\d.]+[kM]?) received", out)
    session = re.findall(r"\$([\d.]+) session", out)
    m = re.search(r"^(?:Main model|Model): (\S+)", out, re.M)
    model, tin, tout = m and m[1], round(sum(n(a) for a, _ in toks)), round(sum(n(b) for _, b in toks))
    return {"cost_usd": float(session[-1]) if session else cost(cfg, model, tin, tout),
            "tokens_in": tin, "tokens_out": tout, "model": model}


def opencode(cfg, out, argv):
    """`opencode run --format json` events: tokens and cost on each step_finish; the model is the -m flag."""
    tin = tout = steps = 0
    usd, error = 0.0, False
    for e in _events(out):
        p = e.get("part") or {}
        if e.get("type") == "step_finish":
            t, cache = p.get("tokens") or {}, (p.get("tokens") or {}).get("cache") or {}
            tin += t.get("input", 0) + cache.get("read", 0) + cache.get("write", 0)
            tout += t.get("output", 0) + t.get("reasoning", 0)
            usd += p.get("cost") or 0
            steps += 1
        error = error or e.get("type") == "error"
    return {"cost_usd": round(usd, 6) if steps else None, "tokens_in": tin, "tokens_out": tout, "turns": steps,
            "model": _flag(argv, "-m", "--model"), "error": error}


def copilot(cfg, out, argv):
    """`copilot --output-format json` events: model and turns. It reports premium requests, not tokens or USD."""
    model, turns, error = None, 0, False
    for e in _events(out):
        t = e.get("type")
        if t == "model.call_start":
            model = (e.get("data") or {}).get("model") or model
        elif t == "assistant.turn_end":
            turns += 1
        elif t == "result":
            error = e.get("exitCode", 0) != 0
    return {"model": model, "turns": turns, "error": error}


def goose(cfg, out, argv):
    """`goose run --output-format json`: one object after its banner, tokens and status under metadata.
    The model is reported as provider/model, so local providers (ollama) are priced at $0."""
    i = out.find("\n{")
    d = _json(out[i + 1:] if i >= 0 else out)
    if not isinstance(d, dict):
        return {"error": True}
    m = d.get("metadata") or {}
    said = [x for x in d.get("messages") or [] if x.get("role") == "assistant"]
    inf = ((said[-1].get("metadata") or {}).get("inference") or {}) if said else {}
    provider = _flag(argv, "", "--provider") or inf.get("provider")
    name = _flag(argv, "", "--model") or inf.get("requestedModel")
    model = f"{provider}/{name}" if provider and name else name
    tin, tout = m.get("input_tokens", 0), m.get("output_tokens", 0)
    return {"cost_usd": cost(cfg, model, tin, tout, m.get("cache_read_input_tokens", 0)), "tokens_in": tin,
            "tokens_out": tout, "turns": len(said), "model": model, "error": m.get("status", "completed") != "completed"}


PARSERS = {"claude-json": claude, "codex-jsonl": codex, "gemini-json": gemini, "aider-text": aider,
           "opencode-json": opencode, "copilot-jsonl": copilot, "goose-json": goose, "none": lambda cfg, out, argv: {}}


def _flag(argv, short, long):
    for i, a in enumerate(argv):
        if a in (short, long) and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(long + "="):
            return a.split("=", 1)[1]
    return None


def _codex_model():
    try:
        f = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
        return tomllib.loads(f.read_text("utf-8")).get("model")
    except (OSError, ValueError):
        return None


LOCAL = ("ollama", "ollama_chat", "lm_studio", "llamafile")  # litellm prefixes for models on your machine
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
NO_COST = {"none", "copilot-jsonl"}  # parsers that never report what a run cost


def free(argv, env):
    """True when an agent runs a model on this machine (an ollama/lm_studio/llamafile model, a localhost endpoint)
    or a model named free (OpenCode's -free, OpenRouter's :free): a run costs $0 before it reports anything."""
    words = [w.split("=", 1)[-1] for w in argv] + [v for k, v in env.items() if "PROXY" not in k.upper()]
    return any(w.split("/")[0] in LOCAL or w.endswith(("-free", ":free")) or any(h in w for h in LOCAL_HOSTS)
               for w in words)


def cost(cfg, model, tin, tout, cached=0):
    """USD from tokens with [prices] from whichagent.toml, else models.dev. None when the price is unknown."""
    if not model:
        return None
    if model.split("/")[0] in LOCAL:
        return 0.0
    p = cfg["prices"].get(model) or _models_dev(cfg["root"]).get(model) or _models_dev(cfg["root"]).get(
        model.split("/")[-1])
    if not p or "input" not in p or "output" not in p:
        return None
    usd = (tin - cached) * p["input"] + cached * p.get("cache_read", p["input"]) + tout * p["output"]
    return round(usd / 1e6, 6)


@functools.cache
def _models_dev(root):
    """{model: cost} from models.dev, cached for 24 hours in .whichagent/prices.json."""
    f = Path(root) / ".whichagent" / "prices.json"
    if not f.exists() or time.time() - f.stat().st_mtime > 86400:
        tmp = f.with_name(f"prices.json.{os.getpid()}-{threading.get_ident()}")
        try:
            with urllib.request.urlopen(PRICES_URL, timeout=15) as r:
                tmp.write_bytes(r.read(50_000_000))  # bounded; a cut-off file fails to parse and prices go unknown
            os.replace(tmp, f)  # whole or not at all: workers parsing at the same time never see half a file
        except OSError:
            tmp.unlink(missing_ok=True)  # offline: keep using the stale copy, if any
    try:
        data = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for provider in ("anthropic", "openai", "google", *data):
        for mid, m in ((data.get(provider) or {}).get("models") or {}).items():
            if m.get("cost"):
                out.setdefault(mid, m["cost"])
    return out


def missing_flags(exe, argv):
    """Flags in argv that the CLI's --help doesn't mention (for `whichagent doctor`)."""
    sub = list(itertools.takewhile(lambda a: not a.startswith("-"), argv[1:]))  # e.g. `codex exec`
    try:
        r = subprocess.run([exe, *sub, "--help"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ["(--help failed)"]
    flags = [a.split("=")[0] for a in argv[1:] if a.startswith("-") and a != "-"]
    return [f for f in flags if not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", r.stdout + r.stderr)]
