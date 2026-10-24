#!/usr/bin/env bash
# LocPilot developer setup for macOS.
#
# Installs project-local dependencies (Python venv via uv, npm packages). It does NOT
# install system tools or download models unless you pass the explicit flags below.
#
#   scripts/setup_macos.sh                   # check tools + install project deps
#   scripts/setup_macos.sh --install-tools   # also `brew install uv node` if missing
#   scripts/setup_macos.sh --install-ollama  # also `brew install ollama`
#   scripts/setup_macos.sh --pull-model      # also `ollama pull $LOCPILOT_MODEL` (~4.7 GB default)
#   scripts/setup_macos.sh --with-rust       # also install Rust via rustup (needed only for desktop builds)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL="${LOCPILOT_MODEL:-qwen2.5-coder:7b}"
INSTALL_TOOLS=0 INSTALL_OLLAMA=0 PULL_MODEL=0 WITH_RUST=0
for arg in "$@"; do
  case "$arg" in
    --install-tools) INSTALL_TOOLS=1 ;;
    --install-ollama) INSTALL_OLLAMA=1 ;;
    --pull-model) PULL_MODEL=1 ;;
    --with-rust) WITH_RUST=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; }

[[ "$(uname -s)" == "Darwin" ]] || { echo "This script targets macOS. See README for Linux/Windows."; exit 1; }

bold "Checking tools"
if xcode-select -p >/dev/null 2>&1; then ok "Xcode Command Line Tools"; else
  fail "Xcode Command Line Tools missing — run: xcode-select --install"; exit 1; fi
command -v git >/dev/null && ok "$(git --version)" || { fail "git missing"; exit 1; }

need_brew() { command -v brew >/dev/null || { fail "Homebrew is required for --install-* flags: https://brew.sh"; exit 1; }; }

if ! command -v uv >/dev/null; then
  if [[ $INSTALL_TOOLS == 1 ]]; then need_brew; brew install uv; else
    fail "uv missing — install with: brew install uv   (or rerun with --install-tools)"; exit 1; fi
fi
ok "$(uv --version)"

if ! command -v node >/dev/null; then
  if [[ $INSTALL_TOOLS == 1 ]]; then need_brew; brew install node; else
    fail "Node.js missing — install Node 20+ (brew install node), or rerun with --install-tools"; exit 1; fi
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$NODE_MAJOR" -ge 20 ]] && ok "node $(node --version)" || { fail "Node 20+ required (found $(node --version))"; exit 1; }

if [[ -f "$HOME/.cargo/env" ]]; then source "$HOME/.cargo/env"; fi
if command -v cargo >/dev/null; then ok "$(cargo --version) (desktop builds)"; else
  if [[ $WITH_RUST == 1 ]]; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal
    source "$HOME/.cargo/env"; ok "$(cargo --version)"
  else warn "Rust not found — only needed to build the desktop app (rerun with --with-rust)"; fi
fi

bold "Installing project dependencies"
(cd "$ROOT/backend" && uv sync --frozen 2>/dev/null || uv sync) && ok "backend virtualenv (backend/.venv)"
(cd "$ROOT/frontend" && npm ci --no-audit --no-fund) && ok "frontend node_modules"

bold "Local model (Ollama)"
if ! command -v ollama >/dev/null && [[ ! -d /Applications/Ollama.app ]]; then
  if [[ $INSTALL_OLLAMA == 1 ]]; then need_brew; brew install ollama; ok "ollama installed"; else
    warn "Ollama not installed. Install the app from https://ollama.com/download/mac or: brew install ollama"
    warn "LocPilot never falls back to cloud APIs — AI features stay disabled until Ollama is running."
  fi
fi
if curl -fsS -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  ok "Ollama is running"
  if curl -fsS http://127.0.0.1:11434/api/tags | grep -q "\"${MODEL}\""; then ok "model ${MODEL} installed"
  elif [[ $PULL_MODEL == 1 ]]; then ollama pull "$MODEL"; ok "model ${MODEL} installed"
  else warn "model ${MODEL} not installed — run: ollama pull ${MODEL}   (or rerun with --pull-model)"; fi
else
  warn "Ollama is not running — start the Ollama app or run: ollama serve"
fi

bold "Done"
echo "  Start LocPilot (browser dev mode):  scripts/dev.sh   → http://127.0.0.1:5420"
echo "  Desktop app (dev):                   cd frontend && npm run tauri dev"
echo "  Run all tests:                       scripts/test.sh"
