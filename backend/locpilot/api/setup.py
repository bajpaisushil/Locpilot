"""Health, first-run dependency checks, model status/config and explicit model downloads."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from locpilot import __version__
from locpilot.api.models import ApiError, ModelConfigUpdate, PullRequest, db_of, settings_of
from locpilot.config import platform_name
from locpilot.llm.launcher import DEFAULT_ADDRESS, LaunchInfo, OllamaInstall, listen_address, ollama_host_value
from locpilot.llm.ollama import (
    MODEL_NAME_RE,
    LLMResponseError,
    LLMUnavailableError,
    OllamaClient,
    physical_memory,
    pick_installed_model,
)
from locpilot.security.sandbox import detect_sandbox
from locpilot.tools import git

router = APIRouter(prefix="/api")
log = logging.getLogger("locpilot")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


async def ensure_ollama(request: Request, wait_s: float = 20.0) -> LaunchInfo:
    """Start the installed Ollama if it is not running (no-op when it runs, isn't installed, or is remote)."""
    return await request.app.state.ollama_launcher.ensure_running(wait_s=wait_s)


async def model_status_with_fallback(request: Request) -> dict:
    """The model status; if the configured model isn't installed but other chat models are, switch to one of them.

    So a model you downloaded yourself (`ollama pull …`) is used without visiting Settings. Not while a download
    started from the setup screen is running: that one is about to provide the configured model.
    """
    llm = request.app.state.llm_factory()
    status = await llm.status()
    if (not isinstance(llm, OllamaClient) or not status["ollama_reachable"] or status["model_available"]
            or request.app.state.pull.get("state") == "pulling"):
        return _with_switch(request, status)
    pick = pick_installed_model(status["models"], physical_memory())
    if pick is None:
        return _with_switch(request, status)
    db_of(request).set_setting("model", pick)
    request.app.state.model_switch = {"from": llm.model, "to": pick}
    log.info("model %s is not installed; using the installed model %s", llm.model, pick)
    return _with_switch(request, await request.app.state.llm_factory().status())


def _with_switch(request: Request, status: dict) -> dict:
    """Adds ``switched_from``: the missing model that LocPilot replaced with the current one (this run), if any."""
    switch = getattr(request.app.state, "model_switch", None)
    replaced = switch["from"] if switch and switch["to"] == status.get("configured_model") else None
    return {**status, "switched_from": replaced}


def _step(title: str, command: str | None = None, url: str | None = None, description: str | None = None) -> dict:
    return {"title": title, "command": command, "url": url, "description": description}


def _install_steps(platform: str) -> list[dict]:
    """How to install Ollama. Once installed, LocPilot starts it by itself."""
    auto = "Once it is installed, LocPilot starts it automatically."
    if platform == "macos":
        return [
            _step("Download and install Ollama for macOS", url="https://ollama.com/download/mac",
                  description=f"LocPilot does not bundle Ollama. Install the Ollama app, or use Homebrew below. {auto}"),
            _step("Or install with Homebrew", command="brew install ollama", description=auto),
        ]
    if platform == "windows":
        return [
            _step("Download and install Ollama for Windows", url="https://ollama.com/download/windows",
                  description=f"LocPilot does not bundle Ollama. {auto}"),
        ]
    return [
        _step("Install Ollama for Linux", url="https://ollama.com/download/linux",
              command="curl -fsSL https://ollama.com/install.sh | sh",
              description="Review the install script before running it, or follow the manual instructions. "
                          "The installer starts Ollama as a system service."),
    ]


def _start_steps(platform: str, install: OllamaInstall, ollama_url: str, log_path: str) -> list[dict]:
    """How to start an installed Ollama by hand (LocPilot tried and could not, or autostart is off)."""
    if install.kind == "service":
        return [_step("Start the Ollama service", command="sudo systemctl start ollama",
                      description="`sudo systemctl enable ollama` also starts it whenever the computer boots.")]
    custom = listen_address(ollama_url) != DEFAULT_ADDRESS
    if install.kind == "app" and not custom:
        return [_step("Open the Ollama app", command="open -a Ollama",
                      description="It runs in the menu bar and keeps running in the background.")]
    serve = f"OLLAMA_HOST={ollama_host_value(ollama_url)} ollama serve" if custom else "ollama serve"
    steps = [_step("Start Ollama", command=serve,
                   description=f"Keep that terminal open while you use LocPilot. Output of the Ollama LocPilot "
                               f"starts is written to {log_path}.")]
    resolved = str(install.path.resolve()) if install.kind == "cli" else ""
    if platform == "macos" and ("/homebrew/" in resolved or "/Cellar/" in resolved) and not custom:
        steps.append(_step("Or keep it running in the background (Homebrew)", command="brew services start ollama",
                           description="Starts Ollama now and whenever you log in."))
    elif platform == "windows":
        steps.append(_step("Or open Ollama from the Start menu", description="It runs in the system tray."))
    return steps


def _model_steps(model: str) -> list[dict]:
    return [
        _step(f"Download the model {model}", command=f"ollama pull {model}",
              description="Downloads several GB once; afterwards LocPilot works fully offline. You can also start "
                          "the download from this screen."),
        _step("Or pick a different installed model in Settings",
              description="Any local Ollama chat model works; 7B+ code models give the best results."),
    ]


def _ollama_check(status: dict, launch: LaunchInfo, install: OllamaInstall | None, log_path: str) -> dict:
    check = {"id": "ollama", "label": "Ollama", "required": True}
    url = status["ollama_url"]
    if status["ollama_reachable"]:
        by = " (started by LocPilot)" if launch.started_by_locpilot else ""
        return {**check, "status": "ok", "detail": f"Ollama {status['ollama_version']} is running at {url}{by}",
                "hint": None}
    where = install.describe() if install else None
    if launch.state == "failed":
        detail = (f"Ollama is installed ({where}), but LocPilot could not start it: {launch.error}" if where
                  else f"LocPilot could not start Ollama: {launch.error}")
        logged = f" LocPilot's attempt is logged in {log_path}." if install and install.kind == "cli" else ""
        return {**check, "status": "error", "detail": detail,
                "hint": f"Start it yourself (see below). LocPilot tries again automatically.{logged}"}
    if launch.state == "starting":
        return {**check, "status": "missing", "detail": f"Starting Ollama ({where})…",
                "hint": "This usually takes a few seconds."}
    if launch.state == "service_stopped":
        return {**check, "status": "missing",
                "detail": f"Ollama is installed as {where}, but the service is not running",
                "hint": "Start it with `sudo systemctl start ollama`."}
    if launch.state == "not_installed" or (launch.state == "disabled" and install is None):
        return {**check, "status": "missing", "detail": "Ollama is not installed on this computer",
                "hint": "Install Ollama (see below). LocPilot starts it automatically once it is installed."}
    if launch.state == "disabled":
        return {**check, "status": "missing", "detail": f"Ollama is installed ({where}) but is not running at {url}",
                "hint": "Start it (see below). LocPilot does not start it itself because "
                        "LOCPILOT_AUTOSTART_OLLAMA is off."}
    # A remote Ollama URL, or Ollama stopped answering between the two checks.
    return {**check, "status": "missing", "detail": status["error"] or f"Ollama is not reachable at {url}",
            "hint": status["hint"]}


@router.get("/setup/status")
async def setup_status(request: Request) -> dict:
    settings = settings_of(request)
    launcher = request.app.state.ollama_launcher
    launch = await ensure_ollama(request)
    status = await model_status_with_fallback(request)
    llm = request.app.state.llm_factory()
    install = None
    if not status["ollama_reachable"] and launch.state != "remote":
        install = launch.install or (None if launch.state == "not_installed" else launcher.installation())
    log_path = str(launcher.log_path)
    checks = [_ollama_check(status, launch, install, log_path)]
    if not status["ollama_reachable"]:
        checks.append({"id": "model", "label": f"Model {llm.model}", "status": "missing", "required": True,
                       "detail": "Waiting for Ollama — the model can be checked once Ollama is running.",
                       "hint": f"ollama pull {llm.model}"})
    elif status["model_available"]:
        info = next((m for m in status["models"] if llm.model_installed(llm.model, [m["name"]])), {})
        size = f" ({info.get('size_bytes', 0) / 1e9:.1f} GB)" if info.get("size_bytes") else ""
        why = (f" — {status['switched_from']} is not installed, so LocPilot uses this model (change it in Settings)"
               if status.get("switched_from") else "")
        checks.append({"id": "model", "label": f"Model {llm.model}", "status": "ok", "required": True,
                       "detail": f"{llm.model} is installed{size}{why}", "hint": None})
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
    platform = platform_name()
    ollama_steps = (_start_steps(platform, install, settings.ollama_url, log_path) if install
                    else _install_steps(platform))
    return {
        "ready": all(c["status"] == "ok" for c in checks if c["required"]),
        "platform": platform, "ollama_url": settings.ollama_url, "model": llm.model,
        "installed_models": [m["name"] for m in status["models"]], "checks": checks,
        "ollama_installed": status["ollama_reachable"] or install is not None,
        "ollama_install": ollama_steps, "model_install": _model_steps(llm.model), "cloud_fallback": False,
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
    await ensure_ollama(request)
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
    await ensure_ollama(request, wait_s=10.0)
    return await model_status_with_fallback(request)


@router.put("/model/config")
async def model_config(body: ModelConfigUpdate, request: Request) -> dict:
    db = db_of(request)
    if body.model is not None:
        previous = request.app.state.llm_factory()
        db.set_setting("model", body.model)
        usage = request.app.state.model_usage
        if body.model != previous.model and previous.model in usage.loaded and isinstance(previous, OllamaClient):
            # Switching models: free the old one's RAM now instead of after the idle timeout.
            usage.forget(previous.model)
            await previous.unload_models([previous.model])
        request.app.state.model_switch = None  # the user chose; no longer LocPilot's substitute
    if body.num_ctx is not None:
        db.set_setting("num_ctx", body.num_ctx)
    if body.temperature is not None:
        db.set_setting("temperature", body.temperature)
    return _with_switch(request, await request.app.state.llm_factory().status())
