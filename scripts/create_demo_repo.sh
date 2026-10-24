#!/usr/bin/env bash
# Create a safe demo repository (a copy of a benchmark fixture) to try LocPilot on.
#   scripts/create_demo_repo.sh [fixture] [destination]
# Defaults: fixture=hotel-offers, destination=~/LocPilotDemo/<fixture>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE="${1:-hotel-offers}"
DEST="${2:-$HOME/LocPilotDemo/$FIXTURE}"
SRC="$ROOT/fixtures/$FIXTURE/repo"
[[ -d "$SRC" ]] || { echo "Unknown fixture '$FIXTURE'. Available: $(ls "$ROOT/fixtures" | tr '\n' ' ')"; exit 1; }
[[ ! -e "$DEST" ]] || { echo "$DEST already exists; choose another destination."; exit 1; }
mkdir -p "$(dirname "$DEST")"
cp -R "$SRC" "$DEST"
cd "$DEST"
git init -q -b main
git add -A
git -c user.name="LocPilot Demo" -c user.email="demo@example.invalid" commit -q -m "Demo project for LocPilot"
echo "Demo repository created at: $DEST"
echo "Try asking: $(python3 -c "import json,sys; print(json.load(open('$ROOT/fixtures/$FIXTURE/benchmark.json'))['prompt'])")"
