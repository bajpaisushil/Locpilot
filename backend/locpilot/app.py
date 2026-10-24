"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from locpilot import __version__
from locpilot.agent.runner import TaskManager
from locpilot.api import repos, setup, tasks, terminals
from locpilot.config import Settings, get_settings
from locpilot.llm.ollama import LLMClient, OllamaClient
from locpilot.security.http import LocalSecurityMiddleware
from locpilot.storage.db import Database
from locpilot.tools.terminal import TerminalManager


def default_llm_factory(settings: Settings, db: Database) -> Callable[[], OllamaClient]:
    def factory() -> OllamaClient:
        model = db.get_setting("model") or settings.model
        num_ctx = db.get_setting("num_ctx") or settings.num_ctx
        temperature = db.get_setting("temperature")
        return OllamaClient(settings.ollama_url, model, num_ctx=int(num_ctx),
                            temperature=settings.temperature if temperature is None else float(temperature),
                            timeout_s=settings.llm_timeout_s, seed=settings.llm_seed,
                            allow_remote=settings.allow_remote_ollama)

    return factory


def create_app(settings: Settings | None = None, *, llm_factory: Callable[[], LLMClient] | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    factory = llm_factory or default_llm_factory(settings, db)
    manager = TaskManager(settings, db, factory)
    terminal_manager = TerminalManager(shell=settings.terminal_shell, max_terminals=settings.max_terminals,
                                       scrollback_chars=settings.terminal_scrollback_chars)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.startup()
        try:
            yield
        finally:
            job = app.state.pull.get("task")
            if job is not None and not job.done():
                job.cancel()
            await terminal_manager.shutdown()
            await manager.shutdown()
            db.close()

    app = FastAPI(title="LocPilot local API", version=__version__, lifespan=lifespan,
                  description="Local-only API for the LocPilot desktop app. See docs/api.md.")
    app.state.settings = settings
    app.state.db = db
    app.state.manager = manager
    app.state.terminals = terminal_manager
    app.state.llm_factory = factory
    app.state.pull = {"model": None, "state": "idle", "status_text": "", "completed": None, "total": None,
                      "error": None}

    app.add_middleware(LocalSecurityMiddleware, allowed_origins=settings.allowed_origins,
                       auth_token=settings.auth_token)
    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-LocPilot-Client", "Last-Event-ID"],
                       max_age=600)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(exc.detail, status_code=exc.status_code)
        return JSONResponse({"error": "http_error", "detail": str(exc.detail), "hint": None},
                            status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return JSONResponse({"error": "invalid_request", "detail": f"{where}: {first.get('msg', 'invalid input')}",
                             "hint": None}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("locpilot").exception("unhandled error")
        return JSONResponse({"error": "internal_error", "detail": f"{type(exc).__name__}", "hint": None},
                            status_code=500)

    app.include_router(setup.router)
    app.include_router(repos.router)
    app.include_router(tasks.router)
    app.include_router(terminals.router)
    return app
