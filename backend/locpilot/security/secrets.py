"""Secret-file exclusion, output redaction and subprocess environment scrubbing."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping

# Files that are never listed, searched, read, shown to the model or edited.
_SECRET_FILE_GLOBS = [
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.p8", "*.ppk",
    "id_rsa", "id_rsa.*", "id_dsa", "id_dsa.*", "id_ecdsa", "id_ecdsa.*", "id_ed25519", "id_ed25519.*",
    ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", ".htpasswd", ".pgpass",
    "credentials", "credentials.json", "credentials.yml", "credentials.yaml", "*.credentials",
    "service-account*.json", "*-service-account.json", "client_secret*.json",
    "secrets.json", "secrets.yml", "secrets.yaml", "secrets.toml", "secrets.ini", "secrets.env",
    "*.tfstate", "*.tfstate.*", "*.tfvars", "kubeconfig", "*.kubeconfig",
    "*.sqlite-journal",
]
_SECRET_ALLOWED = {".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults", ".env.test.example"}
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".kube", ".azure", ".gcloud"}


def is_secret_path(rel_path: str) -> bool:
    """True if a repository-relative path looks like a credential or secret store."""
    norm = rel_path.replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    if any(p.lower() in _SECRET_DIRS for p in parts[:-1]):
        return True
    name = parts[-1].lower()
    if name in _SECRET_ALLOWED:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_FILE_GLOBS)


_REDACTED = "[REDACTED]"

# High-confidence token formats.
_TOKEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
# user:password@ or token@ inside URLs.
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/\s@:]+(?::[^/\s@]*)?)@")
# KEY="literal" assignments with secret-looking names (only quoted literals, to avoid hiding code).
_QUOTED_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<key>[A-Z0-9_\-]*(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|"
    r"access[_-]?token|private[_-]?key|client[_-]?secret)[A-Z0-9_\-]*)(?P<sep>\s*[:=]\s*)"
    r"(?P<q>[\"'])(?P<val>[^\"'\s]{8,})(?P=q)"
)
# KEY=value in command output / env dumps (unquoted). Applied only to process output.
_ENV_ASSIGNMENT = re.compile(
    r"(?im)^(?P<key>[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*)"
    r"(?P<sep>\s*=\s*)(?P<val>\S{6,})$"
)


def redact(text: str, *, process_output: bool = False) -> str:
    """Redact high-confidence secrets from text shown to users, logs or the model."""
    if not text:
        return text
    out = text
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    out = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{_REDACTED}@", out)
    out = _QUOTED_ASSIGNMENT.sub(lambda m: f"{m.group('key')}{m.group('sep')}{m.group('q')}{_REDACTED}{m.group('q')}", out)
    if process_output:
        out = _ENV_ASSIGNMENT.sub(lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", out)
    return out


_SENSITIVE_ENV_NAME = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|SESSION|COOKIE|AUTH)"
)
_SENSITIVE_ENV_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GCLOUD_", "LANGCHAIN_",
                           "LANGSMITH_", "HF_", "HUGGING", "GITHUB_", "GH_", "GITLAB_", "NPM_", "DOCKER_", "KUBE",
                           "LOCPILOT_", "SSH_", "GPG_", "VAULT_", "DATABASE_URL", "PG", "MYSQL_", "REDIS_")
_ENV_KEEP = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TMP",
             "TEMP", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA", "LOCALAPPDATA",
             "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
             "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "XDG_RUNTIME_DIR", "JAVA_HOME", "GOPATH",
             "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "NVM_DIR", "VIRTUAL_ENV", "PYENV_ROOT", "DEVELOPER_DIR",
             "SDKROOT"}


def scrub_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the environment without credentials or tokens.

    Used for every subprocess LocPilot launches on the agent's behalf so that
    project code and tests never see GitHub tokens, cloud keys, SSH agents or
    LocPilot's own API token.
    """
    source = dict(os.environ if env is None else env)
    clean: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if key in _ENV_KEEP or upper in _ENV_KEEP:
            clean[key] = value
            continue
        if _SENSITIVE_ENV_NAME.search(key) or upper.startswith(_SENSITIVE_ENV_PREFIXES):
            continue
        clean[key] = value
    return clean
