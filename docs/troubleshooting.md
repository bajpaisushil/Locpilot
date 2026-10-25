# Troubleshooting

## Setup screen says "Ollama is not reachable"

* Start Ollama: open the **Ollama** app (menu bar) or run `ollama serve`.
  With Homebrew: `brew services start ollama` keeps it running in the background.
* Check it answers: `curl http://127.0.0.1:11434/api/version`.
* Using a non-default address? Set `LOCPILOT_OLLAMA_URL`. LocPilot refuses non-loopback
  addresses unless `LOCPILOT_ALLOW_REMOTE_OLLAMA=true` (it never falls back to a cloud API).

## "Model … is not installed"

* `ollama pull qwen2.5-coder:7b` (≈4.7 GB), or press **Download model** on the setup
  screen (the local Ollama daemon downloads it; progress is shown).
* Prefer another model? `ollama list`, then choose it in **Settings** (or set
  `LOCPILOT_MODEL`). Code-tuned models of 7B+ parameters work best; smaller models
  produce weaker analyses and patches.

## Not enough RAM / very slow answers

* `qwen2.5-coder:7b` (Q4) needs ~6–8 GB free RAM with a 16k context. Close heavy apps
  (Docker, browsers) or lower the context: Settings → context size, or
  `LOCPILOT_NUM_CTX=8192`.
* First request after a while is slow because Ollama loads the model (5–60 s). LocPilot
  asks Ollama to keep it loaded for 30 minutes.
* On 8 GB machines try a smaller model such as `qwen2.5-coder:3b` (lower quality).
* Expect 1–4 minutes for an Ask-mode investigation and 3–8 minutes for a Code-mode fix
  with verification on an Apple M-series laptop.

## Port conflicts

* Browser dev mode uses 8420 (backend) and 5420 (UI). `scripts/dev.sh` refuses to start
  if either is taken and shows the owning process (`lsof -nP -iTCP:8420 -sTCP:LISTEN`).
  Set `LOCPILOT_PORT` to move the backend; the UI port is fixed in `frontend/vite.config.ts`.
* The desktop app picks a free ephemeral port automatically.

## Desktop app shows "Backend failed to start"

* The error panel shows the last lines of the backend log; full logs are in
  `~/Library/Application Support/dev.locpilot.desktop/logs/backend.log` (macOS).
* macOS Gatekeeper may block an unsigned build: right-click the app → Open, or
  `xattr -dr com.apple.quarantine /Applications/LocPilot.app` for a build you made
  yourself. See [desktop.md](desktop.md).
* "Another LocPilot backend is already using …": only one backend may use a data
  directory at a time (two would run the same tasks). Close the other LocPilot window, or
  stop the other backend (for example `scripts/dev.sh` uses `~/.locpilot` too). A restarting
  backend waits up to `LOCPILOT_LOCK_WAIT_S` (15 s) for the previous one to exit.

## "Git is not installed"

* macOS: `xcode-select --install`. Windows: install Git for Windows. Linux: your package
  manager. Restart LocPilot afterwards.

## Tests fail only inside LocPilot

* The sandbox blocks network access and writes outside the worktree. Tests that download
  data, call local databases/services, or write into `$HOME` will fail; LocPilot flags
  "The sandbox may have blocked…" in the command card. Options: make the tests hermetic,
  allow localhost (`LOCPILOT_SANDBOX_ALLOW_LOCALHOST=true`), or disable the sandbox
  (`LOCPILOT_SANDBOX=off`) for trusted repositories.
* The worktree is a fresh checkout: ignored folders like `.venv` and `node_modules` are
  not copied. LocPilot uses your checkout's `.venv/bin/python` when you approve that
  command and links `node_modules` into the worktree; it never installs dependencies.
* Python projects installed in editable mode: LocPilot puts the worktree (and its `src/`)
  first on `PYTHONPATH` so the patched code is what gets tested.

## Dependency installation failed during setup

* Python: `cd backend && uv sync` (uv downloads Python 3.12 if needed).
* Node: `cd frontend && npm ci`. Use Node 20+ (`node --version`).
* Rust (desktop builds only): `source ~/.cargo/env` or install via
  `scripts/setup_macos.sh --with-rust`.

## A task is stuck or the app was closed mid-task

* Tasks that were running when the backend stopped are marked **interrupted**; press
  **Resume** to continue from the last checkpoint (edits are never applied twice).
* **Cancel** stops the current turn and kills any running test process.

## Where is my data?

See "Data locations" in [architecture.md](architecture.md). Deleting a task removes
LocPilot's worktree for it; your own repositories are never deleted (only clones that
LocPilot made can be removed, and only when you ask).
