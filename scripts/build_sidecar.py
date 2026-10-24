#!/usr/bin/env python3
"""Build the LocPilot backend into a single-file sidecar binary for the Tauri app.

Output: frontend/src-tauri/binaries/locpilot-backend-<rust target triple>[.exe]
(Tauri's `externalBin` naming convention). PyInstaller cannot cross-compile, so run
this on each target OS/architecture (the release workflow does exactly that).

Usage:
    python3 scripts/build_sidecar.py                 # build for the host triple + smoke test
    python3 scripts/build_sidecar.py --target x86_64-apple-darwin
    python3 scripts/build_sidecar.py --skip-smoke-test
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BIN_DIR = ROOT / "frontend" / "src-tauri" / "binaries"
NAME = "locpilot-backend"

# Imported dynamically (by name) at runtime, so PyInstaller's static analysis can't see them.
COLLECT_ALL = [
    "tree_sitter_javascript",
    "tree_sitter_typescript",
    "tree_sitter_go",
    "tree_sitter_java",
    "tree_sitter_rust",
]
COLLECT_SUBMODULES = ["locpilot", "uvicorn", "langgraph", "langgraph.checkpoint", "aiosqlite"]
HIDDEN_IMPORTS = ["langgraph.checkpoint.sqlite.aio", "tree_sitter"]
# Never ship optional heavy/telemetry modules we don't use.
EXCLUDES = ["tkinter", "matplotlib", "IPython", "pytest", "PyInstaller"]


def host_triple() -> str:
    try:
        out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine, machine)
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform.startswith("win"):
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-unknown-linux-gnu"


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def build(triple: str) -> Path:
    ext = ".exe" if "windows" in triple else ""
    # Build artifacts live outside backend/ so they never mix with source files.
    work = ROOT / "build" / "sidecar" / "work"
    dist = ROOT / "build" / "sidecar" / "dist"
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(dist, ignore_errors=True)
    work.mkdir(parents=True)
    # Keep build output out of version control without depending on the root .gitignore.
    (ROOT / "build" / ".gitignore").write_text("*\n", encoding="utf-8")
    # A tiny entry script; importing via the package keeps relative imports working.
    entry = work / "locpilot_entry.py"
    entry.write_text("from locpilot.main import main\n\nif __name__ == '__main__':\n    main()\n", encoding="utf-8")

    cmd = [
        "uv", "run", "--project", str(BACKEND), "--frozen",
        "pyinstaller", "--noconfirm", "--clean", "--onefile",
        "--name", NAME,
        "--distpath", str(dist), "--workpath", str(work / "pyinstaller"), "--specpath", str(work),
        "--console",  # stdout carries the READY line; Tauri hides the console window on Windows
    ]
    for mod in COLLECT_ALL:
        cmd += ["--collect-all", mod]
    for mod in COLLECT_SUBMODULES:
        cmd += ["--collect-submodules", mod]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(entry))
    run(cmd, cwd=work)

    built = dist / f"{NAME}{ext}"
    if not built.exists():
        sys.exit(f"PyInstaller did not produce {built}")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / f"{NAME}-{triple}{ext}"
    shutil.copy2(built, target)
    target.chmod(0o755)
    print(f"sidecar: {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


def smoke_test(binary: Path, timeout: float = 120.0) -> None:
    """Start the binary like the desktop shell does and check /api/health with the token."""
    token = "smoke-test-token"
    with tempfile.TemporaryDirectory(prefix="locpilot-smoke-") as data_dir:
        env = {**os.environ, "LOCPILOT_AUTH_TOKEN": token}
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(binary), "--host", "127.0.0.1", "--port", "0", "--data-dir", data_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            port = None
            assert proc.stdout is not None
            while time.monotonic() - started < timeout:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        err = proc.stderr.read() if proc.stderr else ""
                        sys.exit(f"sidecar exited early (code {proc.returncode}):\n{err[-4000:]}")
                    continue
                if line.startswith("LOCPILOT_READY "):
                    port = json.loads(line.split(" ", 1)[1])["port"]
                    break
            if port is None:
                sys.exit("sidecar did not print LOCPILOT_READY in time")
            ready_s = time.monotonic() - started
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=10) as resp:
                health = json.loads(resp.read())
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/setup/status",
                headers={"Authorization": f"Bearer {token}", "X-LocPilot-Client": "1"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                setup = json.loads(resp.read())
            unauth = urllib.request.Request(f"http://127.0.0.1:{port}/api/setup/status")
            try:
                urllib.request.urlopen(unauth, timeout=10)
                sys.exit("sidecar accepted an unauthenticated request although a token was configured")
            except urllib.error.HTTPError as exc:
                unauth_status = exc.code
            print(f"smoke test: ready in {ready_s:.1f}s on port {port}; health={health}; "
                  f"setup.ready={setup.get('ready')}; unauthenticated request -> HTTP {unauth_status}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", help="Rust target triple (default: host)")
    parser.add_argument("--skip-smoke-test", action="store_true")
    args = parser.parse_args()
    if shutil.which("uv") is None:
        sys.exit("uv is required (https://docs.astral.sh/uv/)")
    triple = args.target or host_triple()
    binary = build(triple)
    if not args.skip_smoke_test:
        smoke_test(binary)


if __name__ == "__main__":
    main()
