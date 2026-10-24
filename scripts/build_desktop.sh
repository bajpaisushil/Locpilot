#!/usr/bin/env bash
# Build the LocPilot desktop app for THIS machine's OS/architecture:
#   1. the Python backend as a one-file sidecar binary (PyInstaller, cannot cross-compile)
#   2. the React UI + Tauri shell, bundled into native installers
#
# Output: frontend/src-tauri/target/release/bundle/
#   macOS: macos/LocPilot.app, dmg/LocPilot_<version>_<arch>.dmg
#   Windows: msi/*.msi, nsis/*-setup.exe        Linux: appimage/*.AppImage, deb/*.deb, rpm/*.rpm
#
# Usage: scripts/build_desktop.sh [--bundles app,dmg] [--skip-smoke-test]
# Ollama and models are NOT bundled — users install them separately (first-run setup screen).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLES=""
SIDECAR_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundles) BUNDLES="$2"; shift 2 ;;
    --skip-smoke-test) SIDECAR_ARGS+=("--skip-smoke-test"); shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# rustup may have been installed without editing shell profiles.
if ! command -v cargo >/dev/null 2>&1 && [[ -f "$HOME/.cargo/env" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.cargo/env"
fi
for tool in python3 uv node npm cargo; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: '$tool' is required (see docs/desktop.md)" >&2; exit 1; }
done

echo "==> Syncing backend environment"
uv sync --project "$ROOT/backend" --frozen

echo "==> Building backend sidecar"
python3 "$ROOT/scripts/build_sidecar.py" "${SIDECAR_ARGS[@]+"${SIDECAR_ARGS[@]}"}"

echo "==> Installing frontend dependencies"
cd "$ROOT/frontend"
if [[ ! -d node_modules ]]; then npm ci; fi

echo "==> Building Tauri bundles"
if [[ -n "$BUNDLES" ]]; then
  npm run tauri build -- --bundles "$BUNDLES"
else
  npm run tauri build
fi

echo
echo "Done. Installers are in: $ROOT/frontend/src-tauri/target/release/bundle"
