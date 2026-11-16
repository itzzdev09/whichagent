"""Built-in adapters' output parsers, model prices and flag checks."""
import functools
import itertools
import json
import os
import re
import subprocess
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


def codex(cfg, out, argv):
    """Unverified: `codex exec --json` events, token usage on turn.completed."""
    tin = tout = cached = 0
    error = False
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
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
    """Unverified: `gemini --output-format json`, tokens per model under stats.models."""
    d = _json(out)
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
    return {"cost_usd": total, "tokens_in": tin, "tokens_out": tout, "model": model, "error": bool(d.get("error"))}


def aider(cfg, out, argv):
    """Unverified: aider's "Tokens: ... sent, ... received. Cost: ... $X session." lines."""
    def n(s):
        return float(s[:-1]) * {"k": 1e3, "M": 1e6}[s[-1]] if s[-1] in "kM" else float(s)
    toks = re.findall(r"Tokens: ([\d.]+[kM]?) sent.*?([\d.]+[kM]?) received", out)
    session = re.findall(r"\$([\d.]+) session", out)
    model = re.search(r"^(?:Main model|Model): (\S+)", out, re.M)
    return {"cost_usd": float(session[-1]) if session else None, "tokens_in": round(sum(n(a) for a, _ in toks)),
            "tokens_out": round(sum(n(b) for _, b in toks)), "model": model[1] if model else None}


PARSERS = {"claude-json": claude, "codex-jsonl": codex, "gemini-json": gemini, "aider-text": aider,
           "none": lambda cfg, out, argv: {}}


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


def cost(cfg, model, tin, tout, cached=0):
    """USD from tokens with [prices] from whichagent.toml, else models.dev. None when the price is unknown."""
    if not model:
        return None
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
        try:
            with urllib.request.urlopen(PRICES_URL, timeout=15) as r:
                f.write_bytes(r.read())
        except OSError:
            pass  # offline: keep using the stale copy, if any
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
