"""Share images of a whichagent benchmark, for X, Reddit, LinkedIn and dev.to.

    python scripts/share.py OUT REPO [REPO ...] [--agents a,b,c] [--title TEXT]

Combines the repos' .whichagent/ results like `whichagent report --merge`, then renders PNGs with headless Chrome
or Edge (set WA_BROWSER to pick one), at 2x for sharp text:
  leaderboard.png  1200x675  solve rate with 95% intervals, cost and time per fix   (X, Reddit)
  best.png         1200x675  best at / best for                                     (X thread)
  habits.png       1200x675  how each agent works                                   (X thread)
  square.png       1080x1080 leaderboard and best-at                                (LinkedIn)
  cover.png        1000x420  title and top agents                                   (dev.to cover)
each also as *-light.png, plus the .html each is rendered from. The numbers come from whichagent's own report code.
"""
import argparse
import collections
import datetime
import html
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from whichagent import report, store  # noqa: E402

NAMES = {"claude": "Claude Code", "codex": "Codex CLI", "copilot": "Copilot CLI", "gemini": "Gemini CLI",
         "gemini-lite": "Gemini CLI", "opencode-free": "OpenCode", "aider-qwen7b": "Aider", "qwen-local": "Qwen Code",
         "goose-local": "Goose", "goose": "Goose", "qwen": "Qwen Code", "aider": "Aider", "opencode": "OpenCode"}
REPO = "github.com/itzzdev09/whichagent"
THEMES = {  # dataviz reference palette: one series hue, validated on each surface
    "dark": {"surface": "#1a1a19", "card": "#232322", "text": "#f0efec", "text2": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "base": "#383835", "bar": "#3987e5"},
    "light": {"surface": "#fcfcfb", "card": "#f3f2ee", "text": "#0b0b0b", "text2": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "base": "#c3c2b7", "bar": "#2a78d6"}}
esc = html.escape


def load(repos, agents):
    tasks, results = [], []
    for root in repos:
        d = Path(root) / ".whichagent"
        tasks += [{**t, "root": str(root)} for t in store.tasks(d)]
        results += store.read(d / "results.jsonl")
    if agents:
        results = [r for r in results if r["agent"] in agents]
    return tasks, store.latest(results)


def who(name, rs):
    """Display name and the model it ran, e.g. ("Aider", "qwen2.5-coder:7b")."""
    models = collections.Counter(r.get("model") for r in rs if r.get("model"))
    model = models.most_common(1)[0][0].rsplit("/", 1)[-1] if models else ""
    return NAMES.get(name, name), model


def bars(rows, per, compact=False):
    """Leaderboard rows: name and model, a bar with its 95% interval, the rate, and (unless compact) $ and time."""
    out = []
    for n, s in rows:
        label, model = who(n, per[n])
        lo, hi = (100 * x for x in s["ci"])
        cost = "n/a" if s["per_solved"] is None else report._money(s["per_solved"]) + ("" if s["cost_complete"] else "*")
        extra = "" if compact else (f'<div class="num">{esc(cost)}</div>'
                                    f'<div class="num">{esc(report._secs(s["secs"]))}</div>')
        out.append(
            f'<div class="row"><div class="who"><b>{esc(label)}</b><span>{esc(model)}</span></div>'
            f'<div class="track"><i style="left:25%"></i><i style="left:50%"></i><i style="left:75%"></i>'
            f'<div class="bar" style="width:{100 * s["rate"]:.2f}%"></div>'
            f'<div class="ci" style="left:{lo:.2f}%;width:{hi - lo:.2f}%"></div></div>'
            f'<div class="rate"><b>{report._pct(s["rate"])}</b><span>{s["solved"]}/{s["runs"]}</span></div>{extra}</div>')
    head = "" if compact else ('<div class="row head"><div></div><div class="ticks"><span>0%</span><span>25%</span>'
                               '<span>50%</span><span>75%</span><span>100%</span></div><div>solved</div>'
                               '<div class="num">$ per fix</div><div class="num">median time</div></div>')
    return head + "".join(out)


def table(rows, head=True):
    cells = lambda r, tag: "".join(f"<{tag}>{esc(str(x))}</{tag}>" for x in r)  # noqa: E731
    return ("<table>" + (f"<tr>{cells(rows[0], 'th')}</tr>" if head else "")
            + "".join(f"<tr>{cells(r, 'td')}</tr>" for r in rows[1 if head else 0:]) + "</table>")


def pages(tasks, results, title, note=""):
    rows, pairs, per = report.analyze(tasks, results)
    repos = " + ".join(dict.fromkeys(Path(t["root"]).name for t in tasks))
    when = datetime.date.today().isoformat()
    sub = f"{len(tasks)} real past fixes from {esc(repos)}, each graded by the repo's own tests"
    partial = any(s["per_solved"] is not None and not s["cost_complete"] for _, s in rows)
    foot = (f"<footer><span>whichagent · {REPO}</span><span>95% Wilson intervals · {when}"
            + (" · * cost not reported for every run" if partial else "") + "</span></footer>")
    note = f'<p class="note">{esc(note)}</p>' if note else ""
    def pretty(rs):  # agent names as people know them, longest first so "goose-local" isn't half-replaced
        names = sorted(per, key=len, reverse=True)
        out = []
        for r in rs:
            for n in names:
                r = [x.replace(n, NAMES.get(n, n)) for x in r]
            out.append(r)
        return out
    best = pretty(report.best_at(rows, per, pairs))
    groups = [["kind of task", "tasks", "most solved", "verdict"],  # p-values stay in report.md
              *[[*g[:3], g[3].split(" (p =")[0]] for g in pretty(report.best_for(tasks, per))]]
    habits = report.profile(rows, per)
    habits = [habits[0], *pretty(habits[1:])]
    best_at = "".join(f'<div class="line"><span>{esc(w)}</span><b>{esc(v)}</b></div>' for w, v in best)
    return {
        "leaderboard": (1200, 675, f"<h1>{esc(title)}</h1><p class=sub>{sub}</p>"
                                   f'<div class="board">{bars(rows, per)}</div>{note}{foot}'),
        "best": (1200, 675, f"<h1>Which agent for which job</h1><p class=sub>{sub}</p>"
                            f'<div class="cols"><div><h2>Best at</h2>{best_at}</div>'
                            f'<div><h2>Best for</h2>{table(groups)}</div></div>'
                            f'<p class="note">A lead "within noise" could go either way on this many tasks.</p>{foot}'),
        "habits": (1200, 675, f"<h1>How each agent works</h1><p class=sub>Medians over its runs on {sub}</p>"
                              f"<div class=wide>{table(habits)}</div>"
                              f'<p class="note">Other files: files the real fix didn\'t touch. Tests: runs where it '
                              f"wrote tests of its own.</p>{foot}"),
        "square": (1080, 1080, f"<h1>{esc(title)}</h1><p class=sub>{sub}</p>"
                               f'<div class="board compact">{bars(rows, per, compact=True)}</div>'
                               f'<div class="best">{best_at}</div>{table(groups[:5])}{foot}'),
        "cover": (1000, 420, f'<h1 class="big">{esc(title)}</h1><p class=sub>{sub}</p>'
                             f'<div class="board compact">{bars(rows[:4], per, compact=True)}</div>'
                             f"<footer><span>whichagent · {REPO}</span></footer>"),
    }


CSS = """*{box-sizing:border-box;margin:0}
html,body{width:{w}px;height:{h}px;overflow:hidden}
body{background:{surface};color:{text};font:16px/1.35 "Segoe UI",system-ui,-apple-system,sans-serif;
padding:40px 48px;display:flex;flex-direction:column;gap:14px;font-variant-numeric:tabular-nums}
h1{font-size:34px;font-weight:700;letter-spacing:-.01em}h1.big{font-size:40px}h2{font-size:18px;margin-bottom:8px}
.sub{color:{text2};font-size:17px}.note{color:{muted};font-size:14px}
.board{display:flex;flex-direction:column;gap:9px;margin-top:6px}
.row{display:grid;grid-template-columns:230px 1fr 92px 90px 100px;gap:16px;align-items:center}
.compact .row{grid-template-columns:200px 1fr 92px}
.row.head{color:{muted};font-size:13px}.num{text-align:right}
.who b{display:block;font-size:17px}.who span{color:{muted};font-size:13px}
.track{position:relative;height:22px;border-left:1px solid {base}}
.track i{position:absolute;top:-4px;bottom:-4px;width:1px;background:{grid}}
.bar{position:absolute;left:0;top:2px;height:18px;background:{bar};border-radius:0 4px 4px 0}
.ci{position:absolute;top:10px;height:2px;background:{text2};box-shadow:0 0 0 1px {surface}}
.ci:before,.ci:after{content:"";position:absolute;top:-5px;width:2px;height:12px;background:{text2}}
.ci:before{left:0}.ci:after{right:0}
.rate b{font-size:18px}.rate span{color:{muted};font-size:14px;margin-left:6px}
.ticks{display:flex;justify-content:space-between}
.cols{display:grid;grid-template-columns:1fr 1.25fr;gap:40px;margin-top:8px}
.line{display:flex;flex-direction:column;padding:7px 0;border-bottom:1px solid {grid}}
.line span{color:{muted};font-size:13px}.line b{font-size:17px;font-weight:600}
.best{display:grid;grid-template-columns:1fr 1fr;gap:0 32px;margin-top:8px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid {grid}}
th{color:{muted};font-weight:500;font-size:13px}td{font-size:15px;white-space:nowrap}.wide td{font-size:17px}
footer{margin-top:auto;display:flex;justify-content:space-between;color:{muted};font-size:13px}"""


def browser():
    found = os.environ.get("WA_BROWSER") or next(filter(None, map(shutil.which, (
        "chrome", "google-chrome", "chromium", "chromium-browser", "msedge"))), None)
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        found = found or (p if Path(p).exists() else None)
    return found or sys.exit("No Chrome or Edge found: set WA_BROWSER to a Chromium-based browser")


def render(exe, page, png, w, h):
    with tempfile.TemporaryDirectory() as profile:  # its own profile: works while your browser is open
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                        f"--user-data-dir={profile}", f"--window-size={w},{h}", f"--screenshot={png}",
                        page.resolve().as_uri()], check=True, capture_output=True, timeout=120)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--agents", type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("--title", default="Which coding agent actually fixes the bug?")
    ap.add_argument("--note", default="", help="a footnote under the leaderboard, e.g. how cost was paid")
    a = ap.parse_args()
    tasks, results = load(a.repos, a.agents)
    if not results:
        sys.exit("No results in those repos")
    out, exe = Path(a.out), browser()
    out.mkdir(parents=True, exist_ok=True)
    for name, (w, h, body) in pages(tasks, results, a.title, a.note).items():
        for theme, colors in THEMES.items():
            css = CSS
            for k, v in {**colors, "w": w, "h": h}.items():
                css = css.replace("{" + k + "}", str(v))
            stem = name if theme == "dark" else f"{name}-light"
            page = out / f"{stem}.html"
            page.write_text(f'<!doctype html><meta charset="utf-8"><style>{css}</style><body>{body}</body>', "utf-8")
            render(exe, page, out / f"{stem}.png", w, h)
            print(out / f"{stem}.png")


if __name__ == "__main__":
    main()
