# Troubleshooting

## Setup screen says Ollama is missing or could not be started

LocPilot starts an installed Ollama by itself (on launch, and whenever it finds Ollama stopped):
the Ollama app on macOS if it is installed, otherwise `ollama serve` in the background, which
LocPilot stops again when it quits. So:

* **"Ollama is not installed on this computer"**: install it (the setup screen links the download,
  or `brew install ollama`). Nothing else is needed; LocPilot starts it once it is installed.
* **"…could not start it: …"**: the reason is shown. The output of the `ollama serve` LocPilot ran is
  in `logs/ollama.log` in the data directory (`~/.locpilot`). A common cause is another program
  using port 11434. Start it yourself with `ollama serve` (or `brew services start ollama`) to see
  the error, then click **Check Again**.
* **Ollama installed somewhere unusual?** Point LocPilot at it with `LOCPILOT_OLLAMA_PATH`.
* **Linux system service**: an Ollama installed by the official script runs as a systemd service
  with its own model folder, so LocPilot does not start a second copy as you. Run
  `sudo systemctl start ollama` (and `sudo systemctl enable ollama` to start it at boot).
* Prefer to manage Ollama yourself? Set `LOCPILOT_AUTOSTART_OLLAMA=false`.
* Check it answers: `curl http://127.0.0.1:11434/api/version`.
* Using a non-default address? Set `LOCPILOT_OLLAMA_URL`. LocPilot refuses non-loopback
  addresses unless `LOCPILOT_ALLOW_REMOTE_OLLAMA=true` (it never falls back to a cloud API).

## "Model … is not installed"

* `ollama pull mimo-v2.6:9b-q4_K_M` (≈6.6 GB), or press **Download model** on the setup
  screen (the local Ollama daemon downloads it; progress is shown).
* Prefer another model? `ollama list`, then choose it in **Settings** (or set
  `LOCPILOT_MODEL`). Models of 7B+ parameters work best; smaller models
  produce weaker analyses and patches.
* You only see this when **no** chat model is installed. If the configured model is missing
  but you installed another one yourself (`ollama pull …`), LocPilot switches to it by itself
  (the largest installed chat model that fits in half your RAM; embedding-only models are
  skipped), remembers it, and says so on the setup screen and in Settings.

## Thinking models (MiMo, Qwen3, …)

Some models can reason before they answer ("thinking"), and they do so by default. LocPilot
tells them explicitly, per step, whether to think: `LOCPILOT_THINKING=off` (default) never,
`auto` only in the root-cause analysis and failure diagnosis, `on` in every step. With the
default model, `auto` made each benchmark fix about 1.5× slower without fixing more bugs (see
[benchmark results](benchmark-results.md)), so thinking is off unless you turn it on. When on,
the reasoning gets its own token budget (`LOCPILOT_THINKING_TOKENS`, default 4096) so it cannot
crowd out the answer. If a model is still thinking when the budget runs out, or keeps repeating
the same sentences (small models sometimes reason in circles), LocPilot cuts the reasoning off
and asks again without thinking. Reasoning is slow on a laptop: about 10 tokens per second for
the default model on an M4, so a full budget costs several minutes per step. Models that
cannot think ignore the setting. With `LOCPILOT_DEBUG_LLM=true`, the reasoning is written to
`~/.locpilot/logs/llm-debug.jsonl` next to the prompts and answers.

## Not enough RAM / very slow answers

* The default `mimo-v2.6:9b-q4_K_M` uses about 5.8 GB of RAM with a 16k context (measured with
  `ollama ps` on an Apple M4); leave a couple of GB more free. Close heavy apps (Docker,
  browsers) or lower the context: Settings → context size, or `LOCPILOT_NUM_CTX=8192`.
* Settings shows how much RAM the model uses (or would need) next to what is free, and a task logs a
  warning when loading the model would not fit — that is when your computer would start swapping.
  **Free RAM now** in Settings unloads the model at once; the next message loads it again.
* When LocPilot starts Ollama itself it stores the context in 8 bits (`OLLAMA_KV_CACHE_TYPE=q8_0` with
  flash attention): about half the context memory, no measurable quality loss. Your own `OLLAMA_*`
  variables win; `LOCPILOT_OLLAMA_KV_CACHE_TYPE=""` turns it off. An Ollama you run yourself (or the
  Ollama app) keeps its own settings — set those variables for it to get the same saving.
* On 8 GB machines, lower the context as above, or try a smaller model such as
  `qwen2.5-coder:3b` (lower quality).
* Expect 1–4 minutes for an Ask-mode investigation and 3–8 minutes for a Code-mode fix
  with verification on an Apple M-series laptop.
* First request after a while is slow because Ollama loads the model (5–60 s). LocPilot
  asks Ollama to keep it loaded for 30 minutes after the last request
  (`LOCPILOT_OLLAMA_KEEP_ALIVE`, e.g. `10m`, or `0` to unload after every request).

## Is the model using my RAM when LocPilot is closed?

No. The model file (≈6.6 GB for the default model) only takes disk space until Ollama
loads it, which happens on LocPilot's first request. LocPilot frees it again:

* **when LocPilot quits**: it unloads the models it loaded (or stops the `ollama serve` it
  started, which frees everything). Models another program loaded are left alone;
* **when you switch models** in Settings: the previous one is unloaded right away;
* **when it is idle** for the keep-alive time above, Ollama unloads it by itself.

An idle Ollama with no model loaded uses about 30 MB. To check or free memory by hand:
`ollama ps` (what is loaded), `ollama stop mimo-v2.6:9b-q4_K_M` (unload now),
`brew services stop ollama` (stop a Homebrew-run Ollama; LocPilot starts it again when needed).

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

## LocPilot uses a lot of disk space

Settings → Storage lists what takes space. **Free …** archives the worktrees (full checkouts) of
finished tasks into small files; opening such a task again restores its worktree exactly, so nothing
is lost. **Clean up** removes older saved agent states, command caches, logs and stale index entries,
and compacts the databases — tasks, their history and follow-up messages keep working. The code index
(`<data dir>/index/symbols.sqlite`) is a cache and can be deleted while LocPilot is closed.
