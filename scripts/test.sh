#!/usr/bin/env bash
# Run all automated checks: backend lint + tests, frontend lint + typecheck + tests.
# Pass --bench to also run the real-model agent benchmark (needs Ollama + the model; ~20 min).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status=0
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() { "$@" || status=1; }

step "backend: ruff"
(cd "$ROOT/backend" && run uv run ruff check locpilot tests)
step "backend: pytest"
(cd "$ROOT/backend" && run uv run pytest -q)
step "frontend: lint"
(cd "$ROOT/frontend" && run npm run --silent lint)
step "frontend: typecheck"
(cd "$ROOT/frontend" && run npm run --silent typecheck)
step "frontend: tests"
(cd "$ROOT/frontend" && run npm test --silent)
step "fixtures: validity (bug reproduces, reference fix passes)"
(cd "$ROOT" && run uv run --project backend python scripts/run_benchmark.py --validate-only)

if [[ "${1:-}" == "--bench" ]]; then
  step "agent benchmark (real local model)"
  (cd "$ROOT" && run uv run --project backend python scripts/run_benchmark.py)
fi

if [[ $status == 0 ]]; then printf '\n\033[32mAll checks passed.\033[0m\n'; else printf '\n\033[31mSome checks failed.\033[0m\n'; fi
exit $status
