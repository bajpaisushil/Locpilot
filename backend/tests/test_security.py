import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from locpilot.security.commands import CommandPolicyError, validate_command
from locpilot.security.paths import PathSecurityError, normalize_relpath, safe_join
from locpilot.security.sandbox import detect_sandbox, seatbelt_profile, wrap_argv
from locpilot.security.secrets import is_secret_path, redact, scrub_env
from locpilot.tools.process import run_async, run_sync

# --- path traversal & confinement ----------------------------------------------------------

@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../b", "/etc/passwd", "~/x", "C:\\Windows", "a\x00b",
                                 ".git/config", ".GIT/HEAD", "", "   ", "//server/share"])
def test_normalize_rejects_traversal_and_absolute(bad):
    with pytest.raises(PathSecurityError):
        normalize_relpath(bad)


def test_safe_join_allows_normal_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    assert safe_join(tmp_path, "src/a.py") == (tmp_path / "src" / "a.py").resolve()
    assert safe_join(tmp_path, "./src//a.py") == (tmp_path / "src" / "a.py").resolve()
    assert safe_join(tmp_path, "src\\a.py") == (tmp_path / "src" / "a.py").resolve()


def test_symlink_escape_is_blocked(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    repo = tmp_path / "repo"
    repo.mkdir()
    os.symlink(outside, repo / "link")
    with pytest.raises(PathSecurityError):
        safe_join(repo, "link/secret.txt")


def test_secret_files_blocked(tmp_path):
    for name in (".env", "config/.env.production", "keys/server.pem", "id_rsa", ".npmrc", "terraform.tfstate"):
        assert is_secret_path(name), name
        with pytest.raises(PathSecurityError):
            safe_join(tmp_path, name)
    assert not is_secret_path(".env.example")
    assert not is_secret_path("src/secrets_manager.py")


def test_redaction():
    text = ("token ghp_" + "a" * 36 + " key AKIA" + "B" * 16 + " url https://user:pw@github.com/x "
            "API_KEY = 'supersecretvalue123'\n-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    out = redact(text)
    assert "ghp_" not in out and "AKIA" not in out and "user:pw" not in out
    assert "supersecretvalue123" not in out and "BEGIN RSA PRIVATE KEY" not in out
    assert redact("password = get_password()") == "password = get_password()"
    assert "hunter22" not in redact("DB_PASSWORD=hunter22", process_output=True)


def test_scrub_env_removes_credentials():
    env = scrub_env({"PATH": "/usr/bin", "HOME": "/h", "GITHUB_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "y",
                     "OPENAI_API_KEY": "z", "SSH_AUTH_SOCK": "/tmp/s", "LOCPILOT_AUTH_TOKEN": "t", "LANG": "C",
                     "MY_SERVICE_PASSWORD": "p", "EDITOR": "vim"})
    assert set(env) == {"PATH", "HOME", "LANG", "EDITOR"}


# --- command policy ----------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["python3", "-m", "pytest", "-q"], ["python3", "-m", "unittest", "discover", "-s", "tests"],
    ["node", "--test"], ["npm", "test"], ["npm", "run", "test:unit"], ["go", "test", "./..."],
])
def test_allowed_commands(argv, tmp_path):
    import shutil

    if not shutil.which(argv[0]):
        pytest.skip(f"{argv[0]} not installed")
    validated = validate_command(argv, tmp_path)
    assert Path(validated.argv[0]).is_absolute()
    assert validated.display.startswith(argv[0])


@pytest.mark.parametrize("argv", [
    ["rm", "-rf", "/"], ["curl", "https://example.com"], ["bash", "-c", "echo hi"], ["sh", "-c", "ls"],
    ["python3", "-c", "print(1)"], ["python3", "-m", "http.server"], ["python3", "script.py"],
    ["node", "-e", "1"], ["node", "--test", "--require", "x.js"], ["npm", "install"], ["npm", "run", "deploy"],
    ["npx", "jest"], ["pip", "install", "x"], ["git", "push"], ["python3", "-m", "pytest", "; rm -rf ~"],
    ["python3", "-m", "pytest", "$(whoami)"], ["python3", "-m", "pytest", "/etc/passwd"],
    ["python3", "-m", "pytest", "../../outside"], ["/bin/bash"], ["make", "deploy"], [],
])
def test_rejected_commands(argv, tmp_path):
    with pytest.raises(CommandPolicyError):
        validate_command(argv, tmp_path)


def test_repo_relative_tool_resolution(tmp_path):
    origin = tmp_path / "origin"
    (origin / ".venv" / "bin").mkdir(parents=True)
    py = origin / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    validated = validate_command([".venv/bin/python", "-m", "pytest"], worktree, origin)
    assert validated.argv[0] == str(py)


# --- process execution -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_kills_process_tree(tmp_path):
    script = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    start = time.monotonic()
    result = await run_async([sys.executable, "-c", script], tmp_path, timeout=1.5, env=dict(os.environ),
                             max_output_bytes=10_000)
    assert result.timed_out and result.exit_code is None
    assert time.monotonic() - start < 15


@pytest.mark.asyncio
async def test_output_is_capped_keeping_head_and_tail(tmp_path):
    script = "print('HEAD'); print('x' * 500000); print('TAIL-SUMMARY')"
    result = await run_async([sys.executable, "-c", script], tmp_path, timeout=20, env=dict(os.environ),
                             max_output_bytes=20_000)
    assert result.truncated
    assert result.stdout.startswith("HEAD") and "TAIL-SUMMARY" in result.stdout
    assert len(result.stdout) < 25_000


@pytest.mark.asyncio
async def test_stop_kills_process_tree_and_keeps_output(tmp_path):
    script = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "print('started', flush=True); time.sleep(60)")
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.0, stop.set)
    start = time.monotonic()
    result = await run_async([sys.executable, "-c", script], tmp_path, timeout=30, env=dict(os.environ),
                             max_output_bytes=10_000, stop=stop)
    assert result.cancelled and not result.timed_out and result.exit_code is None
    assert "started" in result.stdout
    assert time.monotonic() - start < 15


@pytest.mark.asyncio
async def test_no_shell_interpolation(tmp_path):
    result = await run_async([sys.executable, "-c", "import sys; print(sys.argv[1])", "$(echo pwned); ls"], tmp_path,
                             timeout=10, env=dict(os.environ), max_output_bytes=10_000)
    assert result.stdout.strip() == "$(echo pwned); ls"


def test_run_sync_reports_missing_program(tmp_path):
    result = run_sync(["definitely-not-a-real-program-xyz"], tmp_path)
    assert result.error and not result.ok


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt sandbox is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_blocks_network_and_outside_writes(tmp_path):
    info = detect_sandbox()
    assert info.available and info.kind == "seatbelt"
    ws, scratch, data = tmp_path / "ws", tmp_path / "scratch", tmp_path / "data"
    for d in (ws, scratch, data):
        d.mkdir()
    outside = Path.home() / f".locpilot-sandbox-test-{os.getpid()}"
    code = (
        "import socket, pathlib\n"
        "pathlib.Path('inside.txt').write_text('ok')\n"
        f"try:\n    pathlib.Path({str(outside)!r}).write_text('bad'); print('OUTSIDE-WRITE')\nexcept OSError: print('outside-denied')\n"
        "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=3); print('NET-OK')\nexcept OSError: print('net-denied')\n"
    )
    argv, kind = wrap_argv([sys.executable, "-c", code], workspace=ws, scratch=scratch, data_dir=data)
    assert kind == "seatbelt"
    result = await run_async(argv, ws, timeout=30, env=dict(os.environ), max_output_bytes=10_000)
    assert "outside-denied" in result.stdout and "net-denied" in result.stdout, result.stdout + result.stderr
    assert (ws / "inside.txt").read_text() == "ok"
    assert not outside.exists()


def test_seatbelt_network_opt_in_keeps_file_confinement(tmp_path):
    ws, scratch, data = tmp_path / "ws", tmp_path / "scratch", tmp_path / "data"
    closed = seatbelt_profile(ws, scratch, data, allow_localhost=False)
    opened = seatbelt_profile(ws, scratch, data, allow_localhost=False, allow_network=True)
    assert "(deny network-outbound (remote ip))" in closed
    assert "network-outbound" not in opened
    for profile in (closed, opened):
        assert "(deny file-write*)" in profile and "(deny file-read*" in profile and "file-read-data" in profile


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt sandbox is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_with_network_still_blocks_outside_writes(tmp_path):
    ws, scratch, data = tmp_path / "ws", tmp_path / "scratch", tmp_path / "data"
    for d in (ws, scratch, data):
        d.mkdir()
    outside = Path.home() / f".locpilot-sandbox-net-test-{os.getpid()}"
    code = (f"import pathlib\ntry:\n    pathlib.Path({str(outside)!r}).write_text('bad'); print('OUTSIDE-WRITE')\n"
            "except OSError: print('outside-denied')\n")
    argv, kind = wrap_argv([sys.executable, "-c", code], workspace=ws, scratch=scratch, data_dir=data,
                           allow_network=True)
    assert kind == "seatbelt"
    result = await run_async(argv, ws, timeout=30, env=dict(os.environ), max_output_bytes=10_000)
    assert "outside-denied" in result.stdout, result.stdout + result.stderr
    assert not outside.exists()


@pytest.mark.skipif(sys.platform != "darwin" or not __import__("shutil").which("node"), reason="macOS + node only")
@pytest.mark.asyncio
async def test_seatbelt_allows_node_module_resolution_inside_data_dir(tmp_path):
    data = tmp_path / "data"
    ws, scratch = data / "worktrees" / "t1", data / "scratch" / "t1"
    (ws / "src").mkdir(parents=True)
    scratch.mkdir(parents=True)
    (data / "locpilot.db").write_text("private")
    (ws / "src" / "m.js").write_text("module.exports = 42\n")
    (ws / "t.js").write_text("console.log('value', require('./src/m'))\n")
    import shutil as _sh

    argv, _ = wrap_argv([_sh.which("node"), "t.js"], workspace=ws, scratch=scratch, data_dir=data)
    result = await run_async(argv, ws, timeout=30, env=dict(os.environ), max_output_bytes=10_000)
    assert result.exit_code == 0 and "value 42" in result.stdout, result.stderr
    argv, _ = wrap_argv(["/bin/cat", str(data / "locpilot.db")], workspace=ws, scratch=scratch, data_dir=data)
    leak = await run_async(argv, ws, timeout=30, env=dict(os.environ), max_output_bytes=10_000)
    assert leak.exit_code != 0 and "private" not in leak.stdout
