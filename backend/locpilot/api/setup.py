"""Health, first-run dependency checks, model status/config and explicit model downloads."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from locpilot import __version__
from locpilot.api.models import ApiError, ModelConfigUpdate, PullRequest, db_of, settings_of
from locpilot.config import platform_name
from locpilot.llm.ollama import MODEL_NAME_RE, LLMResponseError, LLMUnavailableError
from locpilot.security.sandbox import detect_sandbox
from locpilot.tools import git

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


def _install_steps(platform: str, model: str, ollama_url: str) -> tuple[list[dict], list[dict]]:
    address = ollama_url.split("://", 1)[-1]
    def step(title: str, command: str | None = None, url: str | None = None, description: str | None = None) -> dict:
        return {"title": title, "command": command, "url": url, "description": description}

    if platform == "macos":
        ollama = [
            step("Download and install Ollama for macOS", url="https://ollama.com/download/mac",
                 description="LocPilot does not bundle Ollama. Install the Ollama app, or use Homebrew below."),
            step("Or install with Homebrew", command="brew install ollama"),
            step("Start Ollama", command="ollama serve",
                 description=f"Or open the Ollama app — it runs in the menu bar. LocPilot expects it at {address}."),
        ]
    elif platform == "windows":
        ollama = [
            step("Download and install Ollama for Windows", url="https://ollama.com/download/windows",
                 description="LocPilot does not bundle Ollama. After installing, Ollama runs in the system tray."),
            step("Start Ollama", command="ollama serve", description="Only needed if the tray app is not running."),
        ]
    else:
        ollama = [
            step("Install Ollama for Linux", url="https://ollama.com/download/linux",
                 command="curl -fsSL https://ollama.com/install.sh | sh",
                 description="Review the install script before running it, or follow the manual instructions."),
            step("Start Ollama", command="ollama serve", description="Or: sudo systemctl start ollama"),
        ]
    models = [
        step(f"Download the model {model}", command=f"ollama pull {model}",
             description="Downloads several GB once; afterwards LocPilot works fully offline. You can also start "
                         "the download from this screen."),
        step("Or pick a different installed model in Settings",
             description="Any local Ollama chat model works; 7B+ code models give the best results."),
    ]
    return ollama, models


@router.get("/setup/status")
async def setup_status(request: Request) -> dict:
    settings = settings_of(request)
    llm = request.app.state.llm_factory()
    status = await llm.status()
    checks = []
    if status["ollama_reachable"]:
        checks.append({"id": "ollama", "label": "Ollama", "status": "ok", "required": True,
                       "detail": f"Ollama {status['ollama_version']} is running at {status['ollama_url']}",
                       "hint": None})
    else:
        checks.append({"id": "ollama", "label": "Ollama", "status": "missing", "required": True,
                       "detail": status["error"] or "Ollama is not reachable", "hint": status["hint"]})
    if not status["ollama_reachable"]:
        checks.append({"id": "model", "label": f"Model {llm.model}", "status": "missing", "required": True,
                       "detail": "Waiting for Ollama — the model can be checked once Ollama is running.",
                       "hint": f"ollama pull {llm.model}"})
    elif status["model_available"]:
        info = next((m for m in status["models"] if llm.model_installed(llm.model, [m["name"]])), {})
        size = f" ({info.get('size_bytes', 0) / 1e9:.1f} GB)" if info.get("size_bytes") else ""
        checks.append({"id": "model", "label": f"Model {llm.model}", "status": "ok", "required": True,
                       "detail": f"{llm.model} is installed{size}", "hint": None})
    else:
        checks.append({"id": "model", "label": f"Model {llm.model}", "status": "missing", "required": True,
                       "detail": f"{llm.model} is not installed in Ollama", "hint": f"ollama pull {llm.model}"})
    version = await asyncio.to_thread(git.git_version)
    checks.append({"id": "git", "label": "Git", "status": "ok" if version else "missing", "required": True,
                   "detail": version or "Git was not found on PATH",
                   "hint": None if version else "Install Git (macOS: xcode-select --install; Windows: git-scm.com)."})
    sandbox = detect_sandbox()
    checks.append({"id": "sandbox", "label": "Test command sandbox", "status": "ok" if sandbox.available else "warning",
                   "required": False, "detail": sandbox.description,
                   "hint": None if sandbox.available else "Approved test commands will run without OS sandboxing."})
    ollama_steps, model_steps = _install_steps(platform_name(), llm.model, settings.ollama_url)
    return {
        "ready": all(c["status"] == "ok" for c in checks if c["required"]),
        "platform": platform_name(), "ollama_url": settings.ollama_url, "model": llm.model,
        "installed_models": [m["name"] for m in status["models"]], "checks": checks,
        "ollama_install": ollama_steps, "model_install": model_steps, "cloud_fallback": False,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _pull_state(request: Request) -> dict:
    return request.app.state.pull


@router.get("/setup/pull-status")
async def pull_status(request: Request) -> dict:
    job = dict(_pull_state(request))
    job.pop("task", None)
    return job


@router.post("/setup/pull-model")
async def pull_model(body: PullRequest, request: Request) -> dict:
    job = _pull_state(request)
    llm = request.app.state.llm_factory()
    model = body.model or llm.model
    if not MODEL_NAME_RE.match(model):
        raise ApiError(422, "invalid_model", "Invalid model name.")
    if job.get("state") == "pulling":
        raise ApiError(409, "pull_in_progress", f"Already downloading {job.get('model')}.")
    job.update(model=model, state="pulling", status_text="Starting download…", completed=None, total=None, error=None)

    async def run() -> None:
        async def on_update(update: dict) -> None:
            job.update(status_text=update["status_text"], completed=update["completed"], total=update["total"])

        try:
            await llm.pull(model, on_update)
            job.update(state="success", status_text="Download complete")
        except (LLMUnavailableError, LLMResponseError, ValueError) as exc:
            job.update(state="error", error=str(exc), status_text="Download failed")
        except Exception as exc:  # noqa: BLE001
            job.update(state="error", error=f"{type(exc).__name__}: {exc}", status_text="Download failed")

    job["task"] = asyncio.create_task(run())
    return {k: v for k, v in job.items() if k != "task"}


@router.get("/model/status")
async def model_status(request: Request) -> dict:
    return await request.app.state.llm_factory().status()


@router.put("/model/config")
async def model_config(body: ModelConfigUpdate, request: Request) -> dict:
    db = db_of(request)
    if body.model is not None:
        db.set_setting("model", body.model)
    if body.num_ctx is not None:
        db.set_setting("num_ctx", body.num_ctx)
    if body.temperature is not None:
        db.set_setting("temperature", body.temperature)
    return await request.app.state.llm_factory().status()
