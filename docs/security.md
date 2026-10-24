# Security model

LocPilot reads your code, runs commands you approve, and edits files in a separate
worktree. This document states what it does to keep that safe — and what it does **not**
guarantee.

## Boundaries at a glance

| Concern | Control |
|---|---|
| Network exposure | Backend binds `127.0.0.1` only (refuses other hosts). |
| DNS rebinding | `Host` must be `127.0.0.1`, `localhost` or `[::1]`. |
| Websites calling the API (CSRF) | `Origin` allowlist; non-GET requests need `X-LocPilot-Client`; CORS limited to the dev UI and Tauri origins. |
| Other local processes | Desktop: random per-launch bearer token required on every `/api/*` call (except a public `/api/health` readiness probe). Dev mode: `scripts/dev.sh` also uses a per-session token, injected by the Vite proxy (never exposed to browser JS). |
| Arbitrary file access | Only registered repositories; every path goes through `safe_join` (no absolute paths, `..`, NUL, drive/UNC/stream syntax, `.git/` internals, or symlinks escaping the root). Local repos must be inside allowed roots (default: home), never the home dir itself or system/credential dirs. |
| Secrets | `.env*`, keys, `*.pem`, `.npmrc`, `.netrc`, credentials, tfstate… are excluded from listing, search, reading, the model and edits. Output shown to users/model is redacted (tokens, keys, private keys, URL credentials, `PASSWORD=` style lines). |
| Model access to credentials | Tokens are never put in prompts, URLs, logs or persisted messages. Clone URLs with embedded credentials are rejected; private repos use your own Git credential helper or SSH agent. |
| Ask mode | Read-only: tools are read-only; no worktree, no edits, no command execution. Tested by hashing the repo before/after. |
| Code mode edits | Only in a LocPilot-owned `git worktree add --detach` under the data dir; your working tree, index, branch, stash and history are never touched (unless you click **Apply to my repo**, see below). Every patch is shown as a diff and needs explicit approval. |
| Edit integrity | The model proposes SEARCH/REPLACE blocks; LocPilot validates unique matches, path policy (no secrets, no dependency/build dirs, new files only when planned), syntax (ast/Tree-sitter, no execution), computes the diff itself, and writes atomically only if the file still has the expected hash. Existing test files are append-only for the agent. |
| Rollback | Restores originals only for files that still contain LocPilot's content; anything edited by someone else is left alone and reported. |
| Commands | Allowlist of test runners only (`python -m pytest/unittest`, `pytest`, `node --test`, `npm/pnpm/yarn/bun test`, `npx --no-install jest/vitest/mocha`, `go test`, `cargo test --offline`, `mvn -o test`, `gradle test --offline`, `make test/check`, `dotnet test`, `rspec`). No `-c`/`-e` eval, no installs, no shell, no metacharacters, no paths outside the workspace. Each command needs approval; it is re-validated at execution time. |
| Execution limits | Timeout (default 300 s) with process-group kill, output capped (head + tail), credential-scrubbed environment (`GITHUB_TOKEN`, cloud keys, `SSH_AUTH_SOCK`, LocPilot's token… removed), caches redirected to a per-task scratch dir, `CI=true`, offline flags for Go/Cargo/Maven/Gradle. |
| Sandbox | macOS: Seatbelt (`sandbox-exec`) profile — no outbound network (IP and DNS), writes only to the worktree/scratch/temp, no reads of `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, keychains, `.netrc`, or LocPilot's data dir. Linux: bubblewrap when available (read-only root, no network namespace). Windows: none — the UI says so. |
| Git safety | Read commands run with `GIT_OPTIONAL_LOCKS=0` (no index writes). Clone/worktree run with hooks disabled, `protocol.allow=never` except https/ssh, no submodules, LFS smudge skipped. LocPilot never stashes, resets or checks out in your working tree, and never opens PRs (it only links to the PR page). |
| Commit / push (your click only) | Commits are made from the task worktree, only of the files you tick; the worktree stays detached and a branch ref is created (only if it does not exist) or advanced (only from LocPilot's own previous commit for that task), so a branch moved by anyone else, or checked out in any working tree, is never overwritten. Push sends that branch to `origin` with your own Git credentials (credential helper / SSH agent). Git hooks are not run for either. |
| Apply to my repo (your click only) | Writes the ticked files into your working tree via `git apply` after a `--check`: plain file edits, nothing written if any file conflicts; your index, HEAD, branches and commits are untouched. |
| Your own commands (Commands tab) | Typed by you, never by the agent (no agent tool can reach them). Run by `/bin/sh` in the task worktree under the same sandbox as tests (network off unless you switch it on; writes confined to the worktree either way), with the scrubbed environment, a timeout (`LOCPILOT_USER_COMMAND_TIMEOUT_S`, default 900 s) and a Stop button. Their output is shared with the model on your next message, and the UI says so. |
| Terminal | A real login shell on a pseudo-terminal, like VS Code's: **not sandboxed**, with your normal environment (so installs, dev servers and `git push` work) minus LocPilot's own variables (its session token). Opened only from the UI, never by the agent; every call needs the session token. In a Code-mode task it starts in the task worktree; otherwise in the repository folder. Closing it hangs up every process attached to it. POSIX only. |
| Prompt injection | Repository content is treated as untrusted data and delimited in prompts; the system prompt tells the model to ignore embedded instructions. More importantly, the model **cannot do anything dangerous on its own**: tools are read-only, edits and commands require human approval and pass deterministic policy checks. |
| Telemetry | None. LangSmith/LangChain tracing is force-disabled and related API keys are removed from the backend environment at startup. |
| Remote models | Non-loopback Ollama URLs are refused unless `LOCPILOT_ALLOW_REMOTE_OLLAMA=true`. There is no hosted (cloud) model fallback of any kind. |

## What is NOT guaranteed

* **A Git worktree is not a security sandbox.** It only isolates LocPilot's edits from
  your working tree.
* **Running project tests executes project code.** Approve commands only for code you
  trust. The OS sandbox reduces blast radius (network, writes, credential reads) but
  test code still runs with your user account, can read most of your files, and can
  consume CPU/memory. On Windows (and Linux without bubblewrap) there is no sandbox.
* The sandbox profile allows reads outside the denied credential locations, so a
  malicious test could read other files and write them into the worktree (they would then
  appear in diffs/output, which are redacted for known secret formats).
* **The session token is now as powerful as a terminal.** Anything that can obtain it (it lives
  in the app's webview) can open a shell as you. The webview's CSP only runs LocPilot's own
  scripts, and model output is rendered as Markdown without raw HTML, so model or repository
  text cannot inject script; keep it that way when changing the UI.
* Terminals and "network on" runs execute with your full user rights. Only run code you trust.
* Redaction is pattern-based and can miss unusual secret formats.
* The local model can be wrong. Every citation is verified to exist, but a valid citation
  can still support a wrong conclusion; LocPilot labels static analysis separately from
  facts established by execution, and only reports a fix as verified when a regression
  test failed before the fix and passed after it.

## Private repositories

LocPilot never asks for or stores tokens. To clone a private repository, authenticate
Git itself and LocPilot's `git clone` will use it:

* GitHub CLI: `gh auth login && gh auth setup-git` (stores credentials in the macOS
  keychain via the credential helper), or
* SSH: add your key to the SSH agent and use `git@github.com:owner/repo.git`.

Alternatively clone it yourself and add it as a **local repository**.
