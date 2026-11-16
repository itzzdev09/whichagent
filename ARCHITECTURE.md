# whichagent architecture

> Design for v0.1–v0.4. v0.1 is implemented (build order in section 18); later versions are the plan.

## 1. Goal

One command that answers: **which coding agent, with which setup, solves real tasks from my repo, how often, at what cost, and is the difference real?**

Principles:

- **Real tasks only.** Every task is a commit from your history, checked by your own tests. No synthetic bugs.
- **Local.** No service, no account, no upload. Results stay in your repo.
- **Honest numbers.** No answer leaks, no test tampering, an interval on every rate, a significance test on every comparison.
- **Zero dependencies.** Python standard library plus `git`. `gh` and Docker are optional.

Not goals: a hosted leaderboard, building agents, generating training data.

## 2. Pipeline

```mermaid
flowchart TB
    subgraph mine ["whichagent mine"]
        L["git log"] --> F["filter candidates"]
        F --> V["validate: tests fail before the fix, pass after"]
        V --> S["task statement: issue, PR or commit message"]
    end
    S --> T[("tasks.jsonl")]
    T --> Q
    subgraph run ["whichagent run"]
        Q["scheduler: tasks × agents × attempts"] --> W["workspace: leak-free clone at the base commit"]
        W --> O["overlays + setup"]
        O --> A["agent CLI, headless"]
        A --> G["grade: reset tests, add the real tests, run"]
    end
    G --> R[("results.jsonl")]
    R --> P["whichagent report: terminal, HTML, Markdown"]
```

`mine` and `run` append to JSONL files as they go, so every command can be stopped and resumed without losing work.

## 3. Where things live

| What | Where |
|---|---|
| whichagent's code | this repo, `src/whichagent/` |
| Your config | `whichagent.toml` at your repo root (commit it) |
| Tasks, results, logs, diffs, reports | `.whichagent/` in your repo (add it to `.gitignore`) |
| Leak-free base clones | `.whichagent/bases/<task>.git`, one per task, made during `mine` |
| Workspaces | the system temp dir, one per run, deleted after grading (`--keep` to inspect) |
| Model prices | models.dev public JSON, cached for 24 hours in `.whichagent/prices.json` |
| whichagent's own CI | GitHub Actions on Linux, macOS and Windows, with fixture repos and fake agents (no API spend) |
| Releases | PyPI, published from GitHub Actions on version tags (trusted publishing) |

Workspaces sit in the temp dir on purpose: inside a synced folder (OneDrive, Dropbox) every run would trigger a sync of thousands of files.

## 4. Data model

### Task: one line in `.whichagent/tasks.jsonl`

```jsonc
{
  "id": "3f9c2e1",                  // short sha of the fix commit
  "base": "<sha>",                  // parent of the fix: where agents start
  "fix": "<sha>",                   // the real fix
  "date": "2026-09-14",
  "statement": "CSV reader drops the last row when the file has no trailing newline ...",
  "statement_source": "pr",         // issue | pr | commit
  "source_files": ["src/csvkit/reader.py"],
  "test_files": ["tests/test_reader.py"],
  "fix_lines": 14,                  // changed source lines in the real fix
  "mode": "hidden",                 // hidden | visible (see 5.3)
  "suite_green": true,              // full suite passes at the fix commit
  "test_secs": 8.2                  // how long the task's tests take
}
```

### Result: one line in `.whichagent/results.jsonl`

```jsonc
{
  "task": "3f9c2e1", "agent": "codex", "attempt": 1,
  "status": "solved",               // solved | failed | timeout | agent_error | setup_error
  "tests_pass": true,
  "regression": false,              // broke the rest of the suite
  "cost_usd": 0.31, "tokens_in": 182000, "tokens_out": 9100,
  "secs": 245, "turns": 18,
  "files_changed": 2,
  "extra_files": 0,                 // changed files the real fix didn't touch
  "wrote_tests": true,              // added tests of its own (reported, not graded)
  "tampered": false,                // edited existing tests or test config
  "agent_version": "codex-cli 0.x",
  "model": "..."
}
```

Each run also keeps `prompt.txt`, `stdout.log`, `stderr.log`, `agent.diff` and `tests.log` in `.whichagent/runs/<task>/<agent>/<attempt>/`.

## 5. Mining

### 5.1 Candidate filter (cheap: reads history, runs nothing)

`git log --no-merges --numstat` over the window (`since`, default 12 months) on the current branch. A commit is a candidate when:

- it changes at least one test file and at least one source file (decided by `test_patterns`)
- the source change is small: at most `max_files` (5) files and `max_lines` (200) changed lines
- it doesn't touch dependency files (`skip_if_changed`: lockfiles, `requirements*.txt`, ...). A dependency change means the base commit's environment can't run the new tests.
- it has a usable statement (5.4)

Candidates are sorted newest first and capped at `max_candidates` (300).

### 5.2 Validation (expensive: runs tests; parallel; cached in `tasks.jsonl`)

For each candidate, in a fresh workspace at `base` (section 6):

1. run `setup`
2. copy in the fix commit's versions of the task's test files and run the test command on them: it must **fail**
3. copy in the fix commit's source files and run again: it must **pass**
4. run the full suite once and store `suite_green` (this enables the regression check)
5. repeat steps 2–3 `verify_runs` times (default 2); any disagreement means flaky, and the candidate is dropped

Mining stops when `max_tasks` (25) valid tasks are found.

### 5.3 Hidden vs visible tests

- **hidden** (default, SWE-bench style): the agent gets the statement only, and the real tests appear at grading time. This is the realistic case: real tasks don't come with tests.
- **visible**: the agent also gets the new failing tests in its workspace, with an instruction not to edit them.

A task switches to visible when the failure in step 2 shows the tests can't even load: per-stack patterns such as `ImportError`, `errors during collection` or `Cannot find module`. That usually means the fix added a new function or module, and without seeing the tests an agent would have to guess its exact name.

### 5.4 Task statements

The first available of:

1. the linked issue (`Fixes #123` in the commit or PR), via `gh`
2. the PR that merged the commit, via `gh api repos/{owner}/{repo}/commits/{sha}/pulls`
3. the commit message, if it has at least `min_statement_words` (8) words

The prompt is identical for every agent:

```text
Resolve the following task in this repository.

<statement>
```

Visible mode appends the list of failing test files and "Don't edit these files."

Commit messages sometimes describe the fix itself ("use <= instead of <"). That inflates absolute scores but helps every agent equally, so comparisons stay fair. `statement_source` is shown in the report so you can filter.

### 5.5 Contamination

Models may have seen public repos in training. `--since <date>` keeps only commits after that date: use the latest training cutoff among the models you compare. Private repos don't have this problem.

## 6. Workspaces

Every validation and every agent run gets a fresh directory in the system temp dir.

**Leak-free base clones.** During mining, each task gets one bare clone that contains only the history up to `base`:

```bash
git -C <repo> branch whichagent/tmp-<id> <base>
git clone --bare --single-branch --no-tags --depth <history_depth> \
    --branch whichagent/tmp-<id> file://<repo> .whichagent/bases/<id>.git
git -C <repo> branch -D whichagent/tmp-<id>
```

The fix commit and everything after it aren't reachable from `base`, so they never enter the clone. Each workspace is then a fast local clone of that bare repo, with the branch renamed to the repo's default branch and the remote removed. Agents get real `git log` history (default depth 200) and nothing from the future.

**Why not `git worktree`?** Worktrees share the main repo's object store: `git log --all` inside one lists every future commit, including the fix. Agents run `git log` all the time.

**Why outside the repo?** Agents read CLAUDE.md and AGENTS.md from parent directories and sometimes explore `..`. A workspace inside your repo would sit next to today's code, which contains the answer.

**Overlays.** Files from the agent's `overlay` map are copied in after cloning. This is how you A/B a CLAUDE.md, AGENTS.md, MCP config or skill.

**Setup.** `setup` runs in every workspace (`setup_timeout`, default 10 minutes). A failure is recorded as `setup_error` and isn't counted against the agent. v0.1 pays for a full setup per run; the package managers' global caches (uv, npm, pnpm) keep that fast. v0.2 adds a dependency cache keyed by lockfile hash.

## 7. Agent adapters

An agent is data, not code:

```toml
[[agent]]
name    = "claude"
cmd     = "claude -p --output-format json --dangerously-skip-permissions"
stdin   = true              # the prompt goes to stdin
parse   = "claude-json"     # how to read cost, tokens and turns from the output
timeout = "30m"
```

- **Prompt delivery.** Through stdin (`stdin = true`) or a file (`{prompt_file}` in `cmd`), never as a command-line argument. That avoids length limits and the quoting bugs of `.cmd` shims on Windows.
- **Variants.** `extends = "claude"` copies an agent. Add `args = "--model claude-opus-5-5"` to test a model, or `overlay = {...}` to test a setup.
- **Parsers.** One small function per output format: `claude-json`, `codex-jsonl`, `gemini-json`, `aider-text`, and `none` (time and pass/fail only).
- **Prices.** Tokens are converted to dollars with models.dev prices, overridable in `[prices]`. Unknown prices show as `—`, never a guess. On subscription plans, costs are still shown at API prices, which keeps agents comparable on efficiency.
- **Versions.** `<binary> --version` is recorded on every result, so runs from different agent versions are never silently mixed.
- **Your global config.** By default agents read your usual global setup (`~/.claude`, `~/.codex`, ...): you're measuring your setup. To isolate an agent, point its config-dir variable elsewhere in its `env`.

Built-in adapters for v0.1. `whichagent doctor` checks each adapter's flags against the installed CLI's `--help`. The Codex, Gemini CLI and Aider adapters are built from their docs and marked unverified until they've been run.

| Agent | Command | Prompt | Cost source |
|---|---|---|---|
| Claude Code | `claude -p --output-format json --dangerously-skip-permissions` | stdin | `total_cost_usd` in the output |
| Codex CLI | `codex exec --json --full-auto -` (Codex's own sandbox stays on) | stdin | token usage events × prices |
| Gemini CLI | `gemini --output-format json --yolo` | stdin | `stats` tokens × prices |
| Aider | `aider --message-file {prompt_file} --yes-always --no-auto-commits` | file | the "Cost: ... session" line |

Next in line: OpenCode, Cursor CLI, GitHub Copilot CLI, Amp, Goose, Qwen Code.

## 8. Grading

After the agent exits or is killed at its timeout:

1. **Capture.** Stage everything (`git add -A`) and save the diff against the tree the agent started from (`base` plus overlays and, in visible mode, the given tests) as `agent.diff`. Commits the agent made are included; ignored files are not.
2. **Measure.** `files_changed`, `extra_files`, `wrote_tests` (added test lines or files) and `tampered` (removed lines from existing test files, deleted them, or touched test config: `conftest.py`, `pytest.ini`, `jest.config.*`, `vitest.config.*`, ...). Adding a test to an existing file isn't tampering. Edits to `pyproject.toml` or `package.json` are flagged for review rather than reset, since they can be legitimate.
3. **Reset.** Every path matching `test_patterns` or test config goes back to its `base` state (new test files are deleted), then the fix commit's test files are copied in.
4. **Test.** Run the test command on the task's test files (`test_timeout`, default 10 minutes): `tests_pass`.
5. **Regression.** If `suite_green` and the task's tests pass, run the full suite too. Anything red is a regression. (When the task's tests fail, the run has failed anyway.)
6. **Verdict.** `solved = tests_pass and not regression`.

v0.1 grades by the exit code of the task's test files: all of them must pass. v0.2 parses JUnit XML (pytest `--junitxml`, Vitest and Jest reporters, `go test -json`) to get per-test fail-to-pass and pass-to-pass lists, as SWE-bench does.

## 9. Scheduler

- **Matrix.** Tasks × agents × attempts, interleaved task by task. Stopping early (budget, Ctrl-C) still leaves every agent with the same tasks done, so partial results stay comparable.
- **Concurrency.** `jobs` worker threads (all heavy work is subprocesses, so threads are enough) and `jobs_per_agent` to respect subscription rate limits.
- **Resume.** Runs already in `results.jsonl` are skipped. A result is written only when its run finishes. `--retry-errors` reruns runs that ended in `agent_error` or `setup_error` (say, after fixing a login); the newest result counts.
- **Timeouts.** The whole process tree is killed: a new session plus `killpg` on POSIX, `taskkill /T /F` on Windows.
- **Environment.** Agents get an allowlist: PATH, home and temp dirs, locale, the OS variables Windows needs, the provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, ...) and the agent's own `env`. Nothing else from your shell.
- **Budget.** Before starting, whichagent prints the run count and a cost range based on earlier results (or conservative defaults) and asks to continue (`--yes` skips the prompt). During the run, no new run starts once spent money plus the in-flight estimate would pass `max_cost`.

## 10. Statistics

Standard library only (`math`, `random`).

- **Solve rate.** Solved runs ÷ runs, averaged per task. 95% **Wilson interval** when `attempts = 1`, because it stays sane with small samples and near 0% or 100%. With more attempts, a **bootstrap over tasks**, because runs on the same task aren't independent, never narrower than the task-level Wilson interval (on a few tasks that all score 100%, a plain bootstrap would claim 100%–100%).
- **Solved every time.** The share of tasks solved in all `attempts` runs (pass^k): reliability for unattended use.
- **Cost per solved task.** Total cost ÷ solved runs: the number budgets are made of.
- **Paired comparison.** For agents A and B on shared tasks, A wins a task if its solve rate on it is higher. An exact two-sided **sign test** on wins vs losses (ties dropped) gives the verdict: "A is better (p = 0.002)" or "no real difference (p = 0.45)".
- **Small samples.** Under 15 tasks the report says plainly that only large gaps can be detected.

## 11. Reports

- **Terminal**: the leaderboard and the pairwise verdicts.
- **`report.html`**: one self-contained file (inline CSS, JS and SVG, no CDN, works offline, light and dark). It has the leaderboard, a cost-vs-solve-rate chart showing which agents are on the efficient frontier, and a task × agent grid: click a cell to see the agent's diff next to the real fix.
- **`report.md`**: paste into a PR, an issue or a post.
- **JSONL**: `tasks.jsonl` and `results.jsonl` for your own analysis.

## 12. Configuration

`whichagent.toml` sits at the repo root. `whichagent init` writes it from the detected stack, and CLI flags override any key.

```toml
[repo]
setup           = "uv sync"
test            = "uv run pytest -q {files}"   # {files} = the task's test files
test_patterns   = ["tests/**", "**/test_*.py", "**/*_test.py"]
skip_if_changed = ["uv.lock", "requirements*.txt"]

[mine]
since       = "2026-01-01"   # or "12 months"
max_tasks   = 25
max_files   = 5
max_lines   = 200
verify_runs = 2

[run]
agents     = ["claude", "codex", "gemini"]   # default: built-ins found on PATH
attempts   = 1
jobs       = 3
timeout    = "30m"
max_cost   = 25.0            # USD, hard stop
regression = true

[[agent]]
name    = "claude-opus"
extends = "claude"
args    = "--model claude-opus-5-5"

[[agent]]
name    = "claude + new CLAUDE.md"
extends = "claude"
overlay = { "CLAUDE.md" = "experiments/CLAUDE.v2.md" }

[prices]                      # USD per million tokens, overrides models.dev
"some-model" = { input = 1.25, output = 10.0 }
```

Stack detection in v0.1:

| Found | `setup` | `test` |
|---|---|---|
| `uv.lock` | `uv sync` | `uv run pytest -q {files}` |
| `poetry.lock` | `poetry install` | `poetry run pytest -q {files}` |
| `package-lock.json` | `npm ci` | `npx vitest run {files}` or `npx jest {files}` |
| `pnpm-lock.yaml` | `pnpm install --frozen-lockfile` | `pnpm exec vitest run {files}` or `pnpm exec jest {files}` |
| anything else | `init` asks you for the two commands | |

## 13. CLI

```text
whichagent                quickstart: init → mine → estimate → run → report
whichagent init           write whichagent.toml from the detected stack
whichagent doctor         check git, the test command at HEAD, and each agent's flags
whichagent mine           find and validate tasks → .whichagent/tasks.jsonl
whichagent run            run the matrix (resumable) → .whichagent/results.jsonl
whichagent report         terminal table, report.html, report.md
whichagent compare A B    paired comparison of two agents
whichagent show <task>    statement, real fix, each agent's diff
```

## 14. Safety model

Benchmarking means running agents unattended with approvals off. On the host they act with your user's permissions.

v0.1 has:

- throwaway clones outside your repo, so your working tree is never touched
- the environment allowlist (section 9), so secrets in your shell aren't passed through
- timeouts with a process-tree kill, and a hard spend cap
- a one-time confirmation on each machine's first run that says all of this plainly

Not covered in v0.1: files in your home directory are still reachable, and so is the network. An agent could even find your real repo on disk. v0.3 adds Docker mode: setup, agent and grading run in a container built from the repo's `.devcontainer/devcontainer.json` or a Dockerfile, with only the workspace mounted.

## 15. Tech stack

- **Python ≥ 3.11, zero runtime dependencies.** The standard library covers it: `argparse`, `subprocess`, `tomllib`, `json`, `concurrent.futures`, `tempfile`, `shutil`, `math`, `random`, `html`. A tool that runs agents with your permissions should be small enough to audit, with nothing in its supply chain.
- **No install step.** `uvx whichagent` or `pipx run whichagent`.
- **git** through `subprocess` (no GitPython). **gh** is optional, for issue and PR text.
- **None of** async, a database, a plugin system or a web server: threads, subprocesses, JSONL, one HTML file.
- **Platforms.** Linux, macOS and Windows, all three in CI.

## 16. Code layout

```text
src/whichagent/
  cli.py       commands and the quickstart flow                ~210 lines
  config.py    TOML loading, stack defaults, agent extends     ~190
  git.py       log parsing, base clones, workspaces, diffs     ~170
  mine.py      candidates, validation, statements, mode        ~140
  agents.py    built-in adapters, output parsers, prices       ~180
  run.py       scheduler, timeouts, env, budget, resume        ~180
  grade.py     capture, reset, test, verdict                   ~50
  stats.py     Wilson, bootstrap, pass^k, sign test            ~80
  report.py    terminal, Markdown, HTML (inline SVG)           ~260
tests/
  repos.py       builds tiny git repos with known fix commits
  fake_agent.py  fake agents (section 17)
```

About 1,450 lines for v0.1.

## 17. Testing whichagent itself

CI spends nothing on APIs. Tests build small git repos with known bug-fix commits and run the full pipeline against **fake agents**: tiny scripts with known behavior.

| Fake agent | Does | Expected result |
|---|---|---|
| `perfect` | applies the real fix | solved |
| `noop` | nothing | failed |
| `tamper` | edits the test so it passes | failed, `tampered` |
| `breaker` | fixes the bug, breaks another test | failed, regression |
| `sleeper` | sleeps past the timeout | timeout, no processes left behind |
| `peeker` | runs `git log --all` and searches for the fix | finds nothing |

`peeker` is the test behind the "no leaks" claim.

## 18. Build order for v0.1

1. `tests/repos.py` fixture builder and the fake agents (tests first)
2. `config.py` and `git.py`: base clones and workspaces, with `peeker` passing
3. `mine.py`: candidate filter, validation, statements → `tasks.jsonl`
4. `grade.py`, checked against the fake agents
5. `run.py`: matrix, jobs, timeouts, env allowlist, resume, budget
6. `agents.py`: the Claude Code adapter first, then Codex, Gemini CLI and Aider
7. `stats.py` and the terminal report
8. `report.py`: Markdown and HTML
9. `init` stack detection, `doctor` and the quickstart flow
10. Dogfooding on 3 open-source repos (at least one Python, one TypeScript): fix what breaks, then publish to PyPI

## 19. Milestones

| Version | Scope | Done when |
|---|---|---|
| v0.1 "It runs" | everything in section 18 | 20+ valid tasks mined and 3 agents run end to end on 3 real repos, on Linux, macOS and Windows |
| v0.2 "Sharper" | per-test grading (JUnit XML); Go, Rust and Java; dependency cache; shareable SVG results card; comparison across agent versions | the same 3 repos graded per test, plus a Go and a Rust repo |
| v0.3 "Sandboxed" | Docker and devcontainer mode | `peeker` and a filesystem-escape fake agent are both contained |
| v0.4 "Always on" | GitHub Action: reruns on CLAUDE.md or AGENTS.md changes and on a schedule, with a PR comment showing the delta | the action runs on whichagent's own repo |

Later, if people want it: opt-in pooled results across repos ("which agent wins on Django apps?"), rewriting statements to strip solution hints, and tasks that span several commits.

## 20. Known limits and open questions

- **Inline tests** (Rust `#[cfg(test)]`, doctests): tests and code share a file, so a fix can't be split into tests and source by file. v0.1 skips these commits.
- **Monorepos**: one test command may not fit every package. A possible answer is per-path `[repo]` sections; still open.
- **Slow suites**: validation runs tests at least four times per candidate. `max_candidates`, `since` and caching keep this bounded, but a 20-minute suite makes mining slow.
- **Vague statements**: short commit messages make poor tasks. They're filtered by length, and PR or issue text is preferred.
- **Host mode isn't a sandbox**: see section 14.
- **Agent CLIs change often**: adapters are data, versions are recorded, and `doctor` checks the flags.
- **Cost**: a session costs cents to a few dollars, so a full run can reach tens of dollars. The estimate and `max_cost` make that explicit before anything is spent.
