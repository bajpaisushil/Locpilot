.PHONY: setup dev test lint bench fixtures demo desktop sidecar

setup:      ## install project dependencies (see scripts/setup_macos.sh --help)
	scripts/setup_macos.sh

dev:        ## run backend + UI in browser dev mode
	scripts/dev.sh

test:       ## all automated checks (backend, frontend, fixtures)
	scripts/test.sh

lint:
	cd backend && uv run ruff check locpilot tests
	cd frontend && npm run lint && npm run typecheck

bench:      ## real-model agent benchmark (requires Ollama + model)
	uv run --project backend python scripts/run_benchmark.py

fixtures:   ## check that every benchmark fixture is valid
	uv run --project backend python scripts/run_benchmark.py --validate-only

demo:       ## create ~/LocPilotDemo/hotel-offers to try the app on
	scripts/create_demo_repo.sh

sidecar:    ## build the bundled Python backend binary for Tauri
	uv run --project backend python scripts/build_sidecar.py

desktop:    ## build the desktop installer for this OS
	scripts/build_desktop.sh
