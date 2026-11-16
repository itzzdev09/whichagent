"""Leaderboard and pairwise verdicts: terminal text, Markdown, a self-contained HTML page and an SVG card."""
import collections
import datetime
import html
import itertools
import json

from . import git as g, stats, store

SMALL = 15        # under this many tasks, only large gaps are detectable
CAP = 100_000     # characters of each diff embedded in report.html
HEAD = ["agent", "solved", "95% CI", "every time", "$/run", "$/solved", "median time", "issues"]
MARK = {"solved": "✓", "failed": "✗", "timeout": "⏱", "agent_error": "!", "setup_error": "?"}


def label(r, by_version=False):
    return f"{r['agent']} {r.get('agent_version') or '?'}" if by_version else r["agent"]


def analyze(tasks, results, by_version=False):
    """(rows, pairs, per_agent): summaries best first, and every pairwise comparison."""
    ids = {t["id"] for t in tasks}
    per = {}
    for r in results:
        if r["task"] in ids:
            per.setdefault(label(r, by_version), []).append(r)
    rows = [(n, s) for n, rs in per.items() if (s := stats.summary(rs))]
    rows.sort(key=lambda x: (-x[1]["rate"], x[1]["per_solved"] if x[1]["per_solved"] is not None else float("inf")))
    pairs = [(a, b, *stats.compare(per[a], per[b])) for (a, _), (b, _) in itertools.combinations(rows, 2)]
    return rows, pairs, per


def versus(per, baseline, by_version=False):
    """Each agent against its own baseline results on the tasks both ran: [agent, change, record, verdict]."""
    base = {}
    for r in baseline:
        base.setdefault(label(r, by_version), []).append(r)
    out = []
    for n, rs in per.items():
        now, then = stats.summary(rs), stats.summary(base.get(n, []))
        if now and then:
            up, down, _, p = stats.compare(rs, base[n])
            verdict = (f"{'better' if up > down else 'worse'} (p = {p:.2g})" if p < 0.05
                       else f"no real change (p = {p:.2g})")
            out.append([n, f"{_pct(then['rate'])} -> {_pct(now['rate'])}", f"{up} up, {down} down", verdict])
    return out


def _money(x):
    return "-" if x is None else f"${x:.2f}"


def _pct(x):
    return f"{100 * x:.0f}%"


def _issues(s):
    parts = [f"{n} {k.replace('_', ' ')}" for k, n in s["status"].items() if k not in ("solved", "failed")]
    parts += [f"{s[k]} {k}" for k in ("tampered", "regressions", "flagged") if s[k]]
    return ", ".join(parts) or "-"


def _table(rows):
    multi = any(s["every"] is not None for _, s in rows)
    head = [h for h in HEAD if multi or h != "every time"]
    body = []
    for n, s in rows:
        cells = [n, f"{s['solved']}/{s['runs']}", f"{_pct(s['ci'][0])}-{_pct(s['ci'][1])}"]
        if multi:
            cells.append("-" if s["every"] is None else _pct(s["every"]))
        secs = "-" if s["secs"] is None else f"{int(s['secs'] // 60)}m {int(s['secs'] % 60):02d}s"
        cells += [_money(s["per_run"]), _money(s["per_solved"]) + ("" if s["cost_complete"] else "*"), secs,
                  _issues(s)]
        body.append(cells)
    return head, body


def _verdict(a, b, wins, losses, ties, p):
    record = f"{wins} win{'s' * (wins != 1)}, {losses} loss{'es' * (losses != 1)}"
    record += f", {ties} tie{'s' * (ties != 1)}" if ties else ""
    if p < 0.05:
        return [f"{a} vs {b}", record, f"{a if wins > losses else b} is better (p = {p:.2g})"]
    return [f"{a} vs {b}", record, f"no real difference (p = {p:.2g})"]


def _notes(rows, per, by_version):
    notes = []
    n = max((s["tasks"] for _, s in rows), default=0)
    if n < SMALL:
        notes.append(f"Only {n} task{'s' * (n != 1)}: only large gaps can be detected.")
    if not all(s["cost_complete"] for _, s in rows):
        notes.append("* Some runs reported no cost; the $ columns use the runs that did.")
    for name, rs in per.items():
        versions = collections.Counter(r.get("agent_version") for r in rs)
        if len(versions) > 1 and not by_version:
            mix = ", ".join(f"{v} ({k} runs)" for v, k in versions.items())
            notes.append(f"{name} ran as {mix}: --by-version compares them.")
    return notes


def _text(rows):
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(x).ljust(n) for x, n in zip(r, w)).rstrip() for r in rows)


def terminal(tasks, results, baseline=None, by_version=False):
    rows, pairs, per = analyze(tasks, results, by_version)
    if not rows:
        return "No graded runs yet."
    head, body = _table(rows)
    out = [_text([head, *body])]
    if pairs:
        out += ["", _text([_verdict(*p) for p in pairs])]
    if baseline and (vs := versus(per, baseline, by_version)):
        out += ["", "Against the baseline:", _text(vs)]
    return "\n".join(out + [""] + _notes(rows, per, by_version)).rstrip()


def _md(rows):
    rows = [[str(x).replace("|", "\\|") for x in r] for r in rows]
    return "\n".join("| " + " | ".join(r) + " |" for r in [rows[0], ["---"] * len(rows[0]), *rows[1:]])


def _cell(rs):
    return "".join(MARK[r["status"]] for r in sorted(rs, key=lambda r: r["attempt"]))


def _title(t, n=70):
    line = t["statement"].strip().splitlines()[0] if t["statement"].strip() else ""
    return line if len(line) <= n else line[: n - 3] + "..."


def markdown(tasks, results, name, baseline=None, by_version=False):
    rows, pairs, per = analyze(tasks, results, by_version)
    if not rows:
        return f"# whichagent: {name}\n\nNo graded runs yet.\n"
    head, body = _table(rows)
    runs = sum(s["runs"] for _, s in rows)
    md = [f"# whichagent: {name}", "", f"{len(tasks)} tasks, {runs} runs.", "", _md([head, *body])]
    if pairs:
        md += ["", "## Head to head", "", _md([["pair", "record", "verdict"], *[_verdict(*p) for p in pairs]])]
    if baseline and (vs := versus(per, baseline, by_version)):
        md += ["", "## Against the baseline", "", _md([["agent", "solve rate", "tasks", "verdict"], *vs])]
    md += ["", *_notes(rows, per, by_version)]
    names = [n for n, _ in rows]
    grid = [[f"`{t['id']}`", _title(t), *[_cell([r for r in per[n] if r["task"] == t["id"]]) for n in names]]
            for t in tasks]
    md += ["", "## Tasks", "", " ".join(f"{v} {k.replace('_', ' ')}" for k, v in MARK.items()), "",
           _md([["task", "statement", *names], *grid]), ""]
    return "\n".join(md)


def card(tasks, results, name, by_version=False):
    """A small SVG leaderboard to share or put in a README; follows the viewer's light or dark theme."""
    rows, _, _ = analyze(tasks, results, by_version)
    esc, w, h = html.escape, 520, 76 + 30 * len(rows)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
           f'aria-label="whichagent results for {esc(name)}"><style>'
           "text{font:13px system-ui,-apple-system,'Segoe UI',sans-serif;fill:#1f2328}.m{fill:#656d76}"
           ".t{font-size:15px;font-weight:600}.bg{fill:#fff;stroke:#d0d7de}.bar{fill:#0969da}.track{fill:#eaeef2}"
           "@media (prefers-color-scheme:dark){text{fill:#e6edf3}.m{fill:#8d96a0}.bg{fill:#0d1117;stroke:#30363d}"
           ".bar{fill:#4493f8}.track{fill:#21262d}}</style>"
           f'<rect class="bg" x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="8"/>'
           f'<text class="t" x="16" y="28">whichagent · {esc(name)}</text>'
           f'<text class="m" x="16" y="48">{len(tasks)} tasks from this repo\'s history, graded by its own tests</text>']
    for i, (n, s) in enumerate(rows):
        y = 78 + 30 * i
        out.append(f'<text x="16" y="{y}">{esc(n[:24])}</text>'
                   f'<rect class="track" x="200" y="{y - 11}" width="160" height="12" rx="3"/>'
                   f'<rect class="bar" x="200" y="{y - 11}" width="{160 * s["rate"]:.1f}" height="12" rx="3"/>'
                   f'<text x="370" y="{y}">{_pct(s["rate"])}</text>'
                   f'<text class="m" x="420" y="{y}">{_money(s["per_solved"])}/fix</text>')
    return "".join(out) + "</svg>\n"


def _chart(rows):
    pts = [(n, s["per_run"], s["rate"]) for n, s in rows if s["per_run"] is not None]
    if not pts:
        return '<p class="mute">No cost data to chart.</p>'
    W, H, L, R, T, B = 640, 300, 56, 90, 20, 44
    xmax = max(x for _, x, _ in pts) * 1.15 or 1.0

    def X(x):
        return L + x / xmax * (W - L - R)

    def Y(y):
        return H - B - y * (H - T - B)
    front, best = [], -1.0
    for _, x, y in sorted(pts, key=lambda p: (p[1], -p[2])):
        if y > best:
            front.append((x, y))
            best = y
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Solve rate against cost per run">']
    for v in (0, 0.5, 1):
        s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                 f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:.0%}</text>')
    for v in (0, xmax / 2, xmax):
        s.append(f'<text x="{X(v):.1f}" y="{H - B + 18}" text-anchor="middle">${v:.2f}</text>')
    s.append(f'<text x="{(L + W - R) / 2}" y="{H - 6}" text-anchor="middle" class="mute">cost per run</text>')
    if len(front) > 1:
        s.append('<polyline class="front" points="' + " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in front) + '"/>')
    for n, x, y in pts:
        s.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="5"><title>{html.escape(n)}: {y:.0%}, '
                 f'${x:.2f}/run</title></circle><text x="{X(x) + 8:.1f}" y="{Y(y) + 4:.1f}">{html.escape(n)}</text>')
    return "".join(s) + "</svg>"


def page(cfg, tasks, results, by_version=False):
    """report.html: leaderboard, cost chart with the efficient frontier, task grid with diffs side by side."""
    rows, pairs, per = analyze(tasks, results, by_version)
    root, esc = cfg["root"], html.escape
    names = [n for n, _ in rows]
    data = {"tasks": {}, "runs": {}}
    for t in tasks:
        try:
            fix = g.git(root, "show", "--format=", "--no-color", "--no-ext-diff", t["fix"])
        except RuntimeError:
            fix = "(fix commit not found)"
        data["tasks"][t["id"]] = {"statement": t["statement"], "fix": fix[:CAP]}
    for n in names:
        for r in sorted(per[n], key=lambda r: r["attempt"]):
            f = store.run_dir(root, r["task"], r["agent"], r.get("agent_version"), r["attempt"]) / "agent.diff"
            data["runs"].setdefault(f"{r['task']}|{n}", []).append(
                {"attempt": r["attempt"], "status": r["status"],
                 "diff": f.read_text("utf-8", "replace")[:CAP] if f.exists() else ""})
    head, body = _table(rows) if rows else (HEAD, [])
    table = ("<tr>" + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr>"
             + "".join("<tr>" + "".join(f"<td>{esc(str(x))}</td>" for x in r) + "</tr>" for r in body))
    verdicts = "".join("<tr>" + "".join(f"<td>{esc(x)}</td>" for x in _verdict(*p)) + "</tr>" for p in pairs)
    grid = "<tr><th>task</th>" + "".join(f"<th>{esc(n)}</th>" for n in names) + "</tr>"
    for t in tasks:
        grid += f'<tr><td><b>{esc(t["id"])}</b> <span class="mute">{esc(_title(t))}</span></td>'
        for n in names:
            rs = data["runs"].get(f"{t['id']}|{n}")
            if not rs:
                grid += "<td></td>"
                continue
            ok = sum(r["status"] == "solved" for r in rs)
            cls = rs[0]["status"] if len(rs) == 1 else "solved" if ok == len(rs) else "failed" if not ok else "mixed"
            mark = MARK[rs[0]["status"]] if len(rs) == 1 else f"{ok}/{len(rs)}"
            grid += (f'<td class="c {cls}" tabindex="0" data-k="{esc(t["id"])}|{esc(n)}" '
                     f'title="{esc(n)}: {esc(rs[0]["status"])}">{mark}</td>')
        grid += "</tr>"
    notes = "".join(f"<p class='mute'>{esc(x)}</p>" for x in _notes(rows, per, by_version))
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = json.dumps(data).replace("<", "\\u003c")
    runs = sum(s["runs"] for _, s in rows)
    return PAGE.format(name=esc(root.name), when=when, ntasks=len(tasks), nruns=runs, table=table,
                       notes=notes, verdicts=verdicts or "<tr><td>Only one agent: nothing to compare.</td></tr>",
                       chart=_chart(rows), grid=grid, data=payload, js=JS, css=CSS)


CSS = """:root{--bg:#fff;--fg:#1f2328;--mute:#656d76;--line:#d0d7de;--card:#f6f8fa;--ok:#1a7f37;--bad:#cf222e;
--warn:#9a6700;--add:#dafbe1;--del:#ffebe9;--acc:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--line:#30363d;--card:#161b22;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--add:#12261e;--del:#2d1517;--acc:#4493f8}}
*{box-sizing:border-box}body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);
color:var(--fg);max-width:1120px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:32px 0 8px}h4{margin:12px 0 4px;font-size:13px}.mute{color:var(--mute)}
.wrap{overflow-x:auto}table{border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:6px 12px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
td.c{text-align:center;cursor:pointer;font-weight:600}td.c:hover,td.c:focus{background:var(--card);outline:none}
.solved{color:var(--ok)}.failed{color:var(--bad)}.mixed,.timeout,.agent_error,.setup_error{color:var(--warn)}
svg{width:100%;max-width:640px;height:auto}svg text{fill:var(--fg);font-size:12px}svg text.mute{fill:var(--mute)}
svg .grid{stroke:var(--line)}svg .front{stroke:var(--acc);fill:none;stroke-width:1.5;stroke-dasharray:5 4}
svg circle{fill:var(--acc)}.stmt{white-space:pre-wrap;background:var(--card);padding:10px 12px;border-radius:6px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:820px){.cols{grid-template-columns:1fr}}
pre{background:var(--card);padding:10px;overflow:auto;font:12px/1.45 ui-monospace,Consolas,monospace;
max-height:520px;border-radius:6px;margin:0}pre span{display:block;white-space:pre}pre .add{background:var(--add)}
pre .del{background:var(--del)}pre .hunk{color:var(--acc)}"""

JS = """const D = JSON.parse(document.getElementById("data").textContent);
const el = (tag, text, cls) => { const e = document.createElement(tag);
  if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
function pre(text) {
  const p = el("pre");
  for (const l of (text || "(no changes)").replace(/\\n$/, "").split("\\n"))
    p.append(el("span", l || " ", l.startsWith("+") && !l.startsWith("+++") ? "add"
      : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : ""));
  return p;
}
function show(key) {
  const i = key.indexOf("|"), task = key.slice(0, i), agent = key.slice(i + 1), box = document.getElementById("detail");
  const cols = el("div", null, "cols"), fix = el("div"), runs = el("div");
  fix.append(el("h4", "Real fix"), pre(D.tasks[task].fix));
  for (const r of D.runs[key]) runs.append(el("h4", agent + ", attempt " + r.attempt + ": " + r.status), pre(r.diff));
  cols.append(fix, runs);
  box.replaceChildren(el("h2", "Task " + task), el("p", D.tasks[task].statement, "stmt"), cols);
  box.scrollIntoView({behavior: "smooth"});
}
for (const td of document.querySelectorAll("td[data-k]")) {
  td.addEventListener("click", () => show(td.dataset.k));
  td.addEventListener("keydown", e => { if (e.key === "Enter") show(td.dataset.k); });
}"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>whichagent: {name}</title><style>{css}</style></head><body>
<h1>whichagent: {name}</h1>
<p class="mute">{ntasks} tasks, {nruns} runs, {when}. Each task is a past fix from this repo's history, graded by its own tests.</p>
<h2>Leaderboard</h2><div class="wrap"><table>{table}</table></div>{notes}
<h2>Head to head</h2><p class="mute">A wins a task when it solves it more often. Sign test on wins vs losses.</p>
<div class="wrap"><table>{verdicts}</table></div>
<h2>Solve rate vs cost</h2><p class="mute">The dashed line is the efficient frontier: nothing else is both cheaper and better.</p>
{chart}
<h2>Tasks</h2><p class="mute">Click a cell to see the agent's diff next to the real fix.</p>
<div class="wrap"><table>{grid}</table></div>
<div id="detail"></div>
<script type="application/json" id="data">{data}</script>
<script>{js}</script>
</body></html>
"""
