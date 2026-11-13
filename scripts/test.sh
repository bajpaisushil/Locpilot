#!/usr/bin/env bash
# Run all automated checks: backend lint + tests, frontend lint + typecheck + tests.
# Pass --bench to also run the real-model agent benchmark (needs Ollama + the model; ~25 min).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status=0
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "backend: ruff"
(cd "$ROOT/backend" && uv run ruff check locpilot tests) || status=1
step "backend: pytest"
(cd "$ROOT/backend" && uv run pytest -q) || status=1
step "frontend: lint"
(cd "$ROOT/frontend" && npm run --silent lint) || status=1
step "frontend: typecheck"
(cd "$ROOT/frontend" && npm run --silent typecheck) || status=1
step "frontend: tests"
(cd "$ROOT/frontend" && npm test --silent) || status=1
step "fixtures: validity (bug reproduces, reference fix passes)"
(cd "$ROOT" && uv run --project backend python scripts/run_benchmark.py --validate-only) || status=1

if [[ "${1:-}" == "--bench" ]]; then
  step "agent benchmark (real local model)"
  (cd "$ROOT" && uv run --project backend python scripts/run_benchmark.py) || status=1
fi

if [[ $status == 0 ]]; then printf '\n\033[32mAll checks passed.\033[0m\n'; else printf '\n\033[31mSome checks failed.\033[0m\n'; fi
exit $status
