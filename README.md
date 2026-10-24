# LocPilot

**A local-first AI software engineering agent for your Git repositories — as a desktop app.**

LocPilot investigates bugs, explains code with verifiable citations, and — in Code mode —
proposes minimal patches, writes a regression test, and *proves* the fix by running your
tests in an isolated Git worktree. Everything runs on your machine: the React UI in a Tauri
window, a bundled Python agent (FastAPI + LangGraph), and a local model served by
[Ollama](https://ollama.com). There is no hosted backend, no account, no telemetry, and no
cloud model fallback.

> LocPilot is not as capable as frontier hosted models. It is designed for reliability and
> transparency: every claim cites real file lines, static analysis is labelled separately
> from facts established by execution, and a fix is only reported as **verified** when a
> regression test failed before the change and passes after it.

## What it does

* **Connect a repository** — clone a public GitHub/GitLab/Bitbucket URL into LocPilot's
  workspace, or pick an existing local checkout (native folder picker in the desktop app).
  Shows branch, HEAD, and working-tree status; never modifies your checkout.
* **Ask mode (read-only)** — explanations and root-cause analyses grounded in retrieved
  code: execution flow, files/functions/lines examined, hypotheses with confidence and
  supporting/contradicting evidence, most likely root cause (or an explicit "evidence is
  insufficient"), minimal fix, side effects, verification steps, uncertainties. Every
  citation is checked against the repository and opens in the file viewer; unverifiable
  citations are removed and listed.
* **Code mode** — a separate `git worktree` per task; a plan, a validated unified diff and
  the exact test commands are shown for **approval**; then LocPilot applies the new
  regression test alone (expecting it to fail), applies the fix, and runs the tests again in
  a sandbox. Failures are diagnosed from the real output and repaired within strict limits;
  identical failures stop the loop. Rollback, `.patch` download, and follow-ups such as
  "Review your changes for side effects" or "Explain the changes you made".
* **Repository intelligence** — `.gitignore`-aware inventory with secret/dependency/build
  exclusions, literal and regex search, Tree-sitter (JS/TS/Go/Java/Rust) and `ast` (Python)
  symbols, callers/callees and static call chains, imports, manifests and test-runner
  detection, `git log`/blame.
* **Persistent history** — tasks, messages, timelines, reports, diffs and command outputs
  are stored in SQLite; LangGraph checkpoints let interrupted tasks resume after a restart
  without re-applying edits.

## Requirements

| | |
|---|---|
| OS | macOS 11+ (Apple Silicon or Intel) — primary target. Windows 10+/Linux builds are produced by CI but untested by the author. |
| Ollama | Installed separately: https://ollama.com/download (or `brew install ollama`) |
| Model | Default `qwen2.5-coder:7b` (≈4.7 GB download, ~6–8 GB RAM while running). Any local Ollama chat model can be selected. |
| Git | `xcode-select --install` on macOS |
| To build from source | [uv](https://docs.astral.sh/uv/), Node 20+, and Rust (rustup) for the desktop shell |

## Quick start (from source, macOS)

```bash
# 1. project dependencies (Python venv + npm); add flags to install tools/Ollama/model
scripts/setup_macos.sh                    # --install-tools --install-ollama --pull-model --with-rust

# 2. local model (skip if you used --pull-model)
ollama serve &                            # or open the Ollama app
ollama pull qwen2.5-coder:7b

# 3a. run as a desktop app (dev build; needs Rust)
uv run --project backend python scripts/build_sidecar.py   # bundle the Python backend for Tauri
cd frontend && npm run tauri dev

# 3b. …or run in the browser (backend :8420 + UI :5420)
scripts/dev.sh                            # then open http://127.0.0.1:5420
```

Try it on a safe demo project: `scripts/create_demo_repo.sh` creates
`~/LocPilotDemo/hotel-offers` (a small service with a real duplicate-offers bug). Add it as
a local repository and ask, in Ask mode: *"Find the root cause of duplicate hotel offers"*,
then switch to Code mode: *"Fix the bug with the smallest possible change and add a
regression test."*

### Building installers

```bash
scripts/build_desktop.sh      # sidecar (PyInstaller) + `tauri build` → .app/.dmg on macOS
```

OS-specific installers (`.dmg`, `.msi`/`.exe`, `.AppImage`/`.deb`) are built by
[`.github/workflows/release.yml`](.github/workflows/release.yml) when a `v*` tag is pushed and
attached to a **draft** GitHub Release. Ollama and models are never bundled. Signing,
notarization and Gatekeeper/SmartScreen notes: [docs/desktop.md](docs/desktop.md).

## First run

The setup screen checks **Ollama**, the configured **model**, **Git**, and the **test
sandbox**, with live status and a **Check again** button. It shows OS-specific install steps
and copyable commands, and can ask the local Ollama daemon to download the model (explicit
click, with progress). AI features stay disabled until a local model is ready — LocPilot
never silently uses a cloud API. After setup, LocPilot works fully offline (cloning a
remote repository obviously needs the network).

## Using LocPilot

1. **Add repository** → *GitHub URL* (public repos; private repos use your own Git
   credential helper or SSH agent — LocPilot never stores tokens) or *Local folder*.
2. Pick **Ask** (read-only) or **Code** mode in the composer and describe the task.
3. Watch the timeline: understanding → repository inspection → evidence (tool calls,
   snippets) → analysis → citation validation → report. Click any citation to open the file
   at those lines.
4. In Code mode, review the approval card: plan, files, unified diff, proposed commands
   (choose which to run), sandbox status and warnings. **Approve** or **Reject** (optionally
   with feedback for a revised patch).
5. Read the final report: *Fix verified by execution*, *unverified* (and why), or *failed*,
   with real exit codes, durations and test summaries. Inspect the full output in the
   Commands panel and the diff in the Changes panel; download the `.patch`
   (`git apply locpilot-<task>.patch` in your checkout) or roll back.
6. Continue the conversation: "The tests failed. Investigate the failure and try again.",
   "Review your changes for possible side effects.", "Explain the changes you made."

Tasks that were running when the app closed show **Resume**.

## Configuration

All settings are environment variables prefixed with `LOCPILOT_` — see
[`.env.example`](.env.example). Model, context size and temperature can also be changed in
the app's Settings.

## Tests and quality checks

```bash
scripts/test.sh            # backend ruff + pytest, frontend lint + typecheck + vitest, fixture validity
scripts/test.sh --bench    # + real-model agent benchmark (needs Ollama + model, ~20 min)
make bench                 # benchmark only → docs/benchmark-results.md
```

The backend suite uses real Git repositories, worktrees, sandboxed subprocesses, SQLite and
LangGraph checkpoints, with a scripted model stand-in (test-only) for determinism. It covers
URL/path validation, traversal protection, Ask-mode read-only enforcement (repo hashed
before/after), patch validation/apply/unexpected-change detection/rollback, preservation of
the user's uncommitted changes, command allowlist/timeouts/output caps/env scrubbing, the
macOS Seatbelt sandbox, task persistence, restart recovery with a pending approval, resume
of an interrupted turn, cancellation, the HTTP security layer and the SSE stream.

The **agent benchmark** (`fixtures/`, `scripts/run_benchmark.py`) runs the real local model
on four small bug-fix repositories (3 Python, 1 JavaScript) and measures relevant files,
root-cause location/concept, hidden acceptance tests on the patch, whether the agent's
regression test fails on pristine original code and passes on the patch (re-run by the
harness), existing tests, and termination within limits. Latest measured results:
[docs/benchmark-results.md](docs/benchmark-results.md).

## Project layout

```
backend/            Python package `locpilot` (FastAPI, LangGraph agent, tools, security) + tests
frontend/           React + TypeScript + Vite + Tailwind UI (shadcn-style components, Monaco)
frontend/src-tauri/ Tauri v2 desktop shell (spawns and supervises the backend sidecar)
fixtures/           Benchmark/demo repositories with hidden acceptance tests
scripts/            setup, dev, test, benchmark, sidecar/desktop builds, demo repo
docs/               architecture, API contract, security, desktop packaging, troubleshooting, benchmark
.github/workflows/  CI (tests) and release (installers → draft GitHub Release)
```

## Documentation

* [Architecture & agent state machine](docs/architecture.md)
* [API contract (HTTP + SSE)](docs/api.md) — interactive OpenAPI at `http://127.0.0.1:8420/docs` in dev mode
* [Security model](docs/security.md)
* [Desktop packaging & releases](docs/desktop.md)
* [Troubleshooting](docs/troubleshooting.md)
* [Benchmark results](docs/benchmark-results.md)

## Limitations

See the *Status* section of the project report and [docs/security.md](docs/security.md):
small local models make mistakes (LocPilot reports them rather than hiding them); the test
sandbox is macOS Seatbelt / Linux bubblewrap only; Windows/Linux installers are built by CI
but were not tested by hand; semantic (embedding) retrieval is not implemented — retrieval is
lexical + symbol-based.
