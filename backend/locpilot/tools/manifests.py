"""Project structure inspection: manifests, test layout and suggested test commands.

Nothing here executes project code. Test commands are only *suggested*; they
must pass the command policy and be approved by the user before they run.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from locpilot.security.secrets import redact
from locpilot.tools.files import RepoIndex, is_test_path, language_for

MANIFEST_NAMES = [
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt",
    "Pipfile", "tox.ini", "pytest.ini", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "Makefile", "tsconfig.json", "vite.config.ts", "jest.config.js", "vitest.config.ts",
    "deno.json", "mix.exs", "CMakeLists.txt",
]


@dataclass
class ProjectOverview:
    languages: list[dict] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    test_frameworks: list[str] = field(default_factory=list)
    test_commands: list[list[str]] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    readme_excerpt: str = ""
    top_level: list[str] = field(default_factory=list)
    package_info: dict = field(default_factory=dict)
    file_count: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    def prompt_summary(self, max_chars: int = 2500) -> str:
        parts = [
            f"Files indexed: {self.file_count}",
            "Languages: " + ", ".join(f"{lang['name']} ({lang['files']})" for lang in self.languages[:6]),
            "Manifests: " + (", ".join(self.manifests[:15]) or "none"),
            "Top-level entries: " + ", ".join(self.top_level[:30]),
            "Test frameworks: " + (", ".join(self.test_frameworks) or "unknown"),
            "Test files (sample): " + (", ".join(self.test_files[:12]) or "none found"),
        ]
        if self.package_info:
            parts.append("Package info: " + json.dumps(self.package_info)[:600])
        if self.readme_excerpt:
            parts.append("README excerpt:\n" + self.readme_excerpt[:900])
        return "\n".join(parts)[:max_chars]


def _read_json(index: RepoIndex, rel: str) -> dict:
    text = index.read_text(rel)
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _read_toml(index: RepoIndex, rel: str) -> dict:
    text = index.read_text(rel)
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def python_interpreter(repo_root: Path) -> list[str]:
    """Prefer the repository's own virtualenv; otherwise a python on PATH."""
    for rel in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
        if (repo_root / rel).exists():
            return [rel]
    for name in ("python3", "python"):
        if shutil.which(name):
            return [name]
    return ["python3"]


def _venv_has_module(repo_root: Path, module: str) -> bool | None:
    for venv in (".venv", "venv"):
        lib = repo_root / venv / "lib"
        if lib.is_dir():
            return any(lib.glob(f"python3*/site-packages/{module}")) or any(lib.glob(f"python3*/site-packages/{module}.py"))
        win = repo_root / venv / "Lib" / "site-packages" / module
        if (repo_root / venv / "Lib").is_dir():
            return win.exists()
    return None


def inspect_project(index: RepoIndex, origin_root: Path | None = None) -> ProjectOverview:
    """Build an overview of the project. ``origin_root`` is the user's checkout (for venv/node_modules)."""
    index.refresh()
    root_for_tools = origin_root or index.root
    ov = ProjectOverview(file_count=len(index.files), languages=index.language_stats()[:10])
    files = index.files
    file_set = set(files)
    ov.manifests = [f for f in files if f.rsplit("/", 1)[-1] in MANIFEST_NAMES and f.count("/") <= 2][:30]
    ov.test_files = [f for f in files if is_test_path(f) and language_for(f)][:400]
    top: dict[str, None] = {}
    for f in files:
        top.setdefault(f.split("/", 1)[0] + ("/" if "/" in f else ""), None)
    ov.top_level = list(top)[:60]
    for readme in ("README.md", "README.rst", "README.txt", "README"):
        if readme in file_set:
            text = index.read_text(readme) or ""
            ov.readme_excerpt = redact("\n".join(text.splitlines()[:40]))[:1500]
            break

    frameworks: list[str] = []
    commands: list[list[str]] = []

    # Python ---------------------------------------------------------------------------
    py_files = [f for f in files if f.endswith(".py")]
    if py_files:
        pyproject = _read_toml(index, "pyproject.toml") if "pyproject.toml" in file_set else {}
        project = pyproject.get("project", {}) if isinstance(pyproject, dict) else {}
        if project:
            ov.package_info["python_project"] = {"name": project.get("name"),
                                                  "dependencies": list(project.get("dependencies", []))[:15]}
        deps_blob = json.dumps(pyproject) + "".join(index.read_text(f) or "" for f in files
                                                    if re.match(r"(requirements.*\.txt|setup\.cfg|tox\.ini|Pipfile)$", f.rsplit("/", 1)[-1]))
        uses_pytest = (
            "pytest" in deps_blob or "pytest.ini" in file_set or "conftest.py" in {f.rsplit("/", 1)[-1] for f in files}
            or any("import pytest" in (index.read_text(f) or "") for f in ov.test_files[:40] if f.endswith(".py"))
        )
        py_tests = [f for f in ov.test_files if f.endswith(".py")]
        interpreter = python_interpreter(root_for_tools)
        venv_pytest = _venv_has_module(root_for_tools, "pytest")
        if py_tests:
            if uses_pytest and venv_pytest is not False:
                frameworks.append("pytest")
                commands.append([*interpreter, "-m", "pytest", "-q"])
            else:
                frameworks.append("unittest")
                test_dir = "tests" if any(f.startswith("tests/") for f in py_tests) else "."
                commands.append([*interpreter, "-m", "unittest", "discover", "-s", test_dir, "-t", "."])
                if uses_pytest:
                    commands.append([*interpreter, "-m", "pytest", "-q"])

    # JavaScript / TypeScript ---------------------------------------------------------
    if "package.json" in file_set:
        pkg = _read_json(index, "package.json")
        scripts = pkg.get("scripts", {}) if isinstance(pkg.get("scripts"), dict) else {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        ov.package_info["node_package"] = {"name": pkg.get("name"), "scripts": dict(list(scripts.items())[:12]),
                                           "test_deps": [d for d in deps if d in ("jest", "vitest", "mocha", "ava")]}
        runner = "npm"
        if (index.root / "pnpm-lock.yaml").exists():
            runner = "pnpm"
        elif (index.root / "yarn.lock").exists():
            runner = "yarn"
        test_script = str(scripts.get("test", ""))
        if "vitest" in deps:
            frameworks.append("vitest")
        if "jest" in deps:
            frameworks.append("jest")
        if "mocha" in deps:
            frameworks.append("mocha")
        if "node --test" in test_script or any("node:test" in (index.read_text(f) or "") for f in ov.test_files[:30]
                                               if f.endswith((".js", ".mjs", ".cjs", ".ts"))):
            frameworks.append("node:test")
        if test_script and "no test specified" not in test_script:
            commands.append([runner, "test"] if runner != "yarn" else ["yarn", "test"])
        elif "node:test" in frameworks:
            commands.append(["node", "--test"])

    # Go / Rust / JVM / others ------------------------------------------------------
    if "go.mod" in file_set:
        frameworks.append("go test")
        commands.append(["go", "test", "./..."])
    if "Cargo.toml" in file_set:
        frameworks.append("cargo test")
        commands.append(["cargo", "test", "--offline"])
    if "pom.xml" in file_set:
        frameworks.append("maven")
        commands.append(["mvn", "-o", "-q", "test"])
    if "build.gradle" in file_set or "build.gradle.kts" in file_set:
        frameworks.append("gradle")
        commands.append(["./gradlew", "test", "--offline"] if "gradlew" in file_set else ["gradle", "test", "--offline"])
    if "Makefile" in file_set and not commands:
        text = index.read_text("Makefile") or ""
        if re.search(r"^test:", text, re.MULTILINE):
            commands.append(["make", "test"])

    seen: set[tuple[str, ...]] = set()
    ov.test_commands = [c for c in commands if not (tuple(c) in seen or seen.add(tuple(c)))]
    ov.test_frameworks = list(dict.fromkeys(frameworks))
    ov.entry_points = [f for f in files if f.rsplit("/", 1)[-1] in ("main.py", "app.py", "server.py", "index.ts",
                                                                       "index.js", "main.go", "main.rs", "manage.py")][:10]
    return ov
