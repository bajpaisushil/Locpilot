"""Request models and shared API helpers."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from locpilot.agent.runner import TaskManager
from locpilot.config import Settings
from locpilot.storage.db import Database


class ApiError(HTTPException):
    def __init__(self, status: int, error: str, detail: str, hint: str | None = None) -> None:
        super().__init__(status_code=status, detail={"error": error, "detail": detail, "hint": hint})


class RepoCreate(BaseModel):
    source: Literal["github", "local"]
    url: str | None = Field(None, max_length=400)
    path: str | None = Field(None, max_length=4096)


class TaskCreate(BaseModel):
    repo_id: str = Field(min_length=1, max_length=64)
    mode: Literal["ask", "code"]
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        return value


class MessageCreate(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    mode: Literal["ask", "code"] | None = None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(None, max_length=2000)
    commands: list[list[str]] | None = Field(None, max_length=10)


class UserCommandCreate(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    allow_network: bool = False

    @field_validator("command")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("command must not be empty")
        if "\x00" in value:
            raise ValueError("command must not contain NUL bytes")
        return value


class FileSelection(BaseModel):
    files: list[str] = Field(min_length=1, max_length=500)


class CommitCreate(FileSelection):
    message: str = Field(min_length=1, max_length=5000)
    branch: str = Field(min_length=1, max_length=200)

    @field_validator("message", "branch")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class ModelConfigUpdate(BaseModel):
    model: str | None = Field(None, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-/:]*$")
    num_ctx: int | None = Field(None, ge=2048, le=131072)
    temperature: float | None = Field(None, ge=0.0, le=1.0)


class PullRequest(BaseModel):
    model: str | None = Field(None, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-/:]*$")


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def db_of(request: Request) -> Database:
    return request.app.state.db


def manager_of(request: Request) -> TaskManager:
    return request.app.state.manager
