"""Ollama HTTP client.

LocPilot talks to a *local* Ollama daemon only. Non-loopback URLs are refused
unless ``LOCPILOT_ALLOW_REMOTE_OLLAMA=true`` is set explicitly, and there is
no fallback to any hosted model API: if Ollama or the model is missing, calls
fail with :class:`LLMUnavailableError` and the UI shows setup instructions.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from locpilot.config import ollama_url_is_local

T = TypeVar("T", bound=BaseModel)
ProgressCallback = Callable[[int], Awaitable[None]]
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,199}$")


class LLMUnavailableError(RuntimeError):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class LLMResponseError(RuntimeError):
    pass


@dataclass
class LLMResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    model: str
    truncated: bool = False


class LLMClient(Protocol):
    model: str

    async def chat(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = 1024,
                   on_progress: ProgressCallback | None = None, temperature: float | None = None) -> LLMResult: ...


def inline_refs(schema: dict) -> dict:
    """Inline ``$ref``/``$defs`` (Pydantic output) so grammar conversion is simple and reliable."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return resolve(defs[name])
            out = {}
            for key, value in node.items():
                if key in ("$defs", "title", "default"):
                    continue  # schema metadata, not data
                if key == "properties" and isinstance(value, dict):
                    out[key] = {name: resolve(sub) for name, sub in value.items()}  # keep every property name
                else:
                    out[key] = resolve(value)
            return out
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start: end + 1])
        raise


def setup_hint(base_url: str, model: str) -> str:
    return (f"Start Ollama (open the Ollama app or run `ollama serve`) so it listens on {base_url}, "
            f"then download the model with `ollama pull {model}`.")


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, num_ctx: int = 16384, temperature: float = 0.1,
                 timeout_s: float = 900.0, seed: int | None = 7, allow_remote: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.seed = seed
        self.allow_remote = allow_remote

    def _check_local(self) -> None:
        if not self.allow_remote and not ollama_url_is_local(self.base_url):
            raise LLMUnavailableError(
                f"Refusing to use non-local Ollama endpoint {self.base_url}",
                "LocPilot only uses a local model by default. Set LOCPILOT_ALLOW_REMOTE_OLLAMA=true to opt in.",
            )

    async def version(self) -> str | None:
        self._check_local()
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            resp = await client.get(f"{self.base_url}/api/version")
            resp.raise_for_status()
            return resp.json().get("version")

    async def list_models(self) -> list[dict]:
        self._check_local()
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            models = []
            for m in resp.json().get("models", []):
                details = m.get("details") or {}
                models.append({"name": m.get("name") or m.get("model"), "size_bytes": m.get("size"),
                               "parameter_size": details.get("parameter_size"),
                               "quantization": details.get("quantization_level"), "family": details.get("family")})
            return models

    @staticmethod
    def model_installed(model: str, names: list[str]) -> bool:
        candidates = {model, model if ":" in model else f"{model}:latest"}
        return any(n in candidates for n in names)

    async def status(self) -> dict:
        info: dict[str, Any] = {"ollama_reachable": False, "ollama_version": None, "ollama_url": self.base_url,
                                "configured_model": self.model, "model_available": False, "models": [],
                                "num_ctx": self.num_ctx, "temperature": self.temperature, "error": None, "hint": None}
        try:
            info["ollama_version"] = await self.version()
            info["ollama_reachable"] = True
            info["models"] = await self.list_models()
        except LLMUnavailableError as exc:
            info["error"], info["hint"] = str(exc), exc.hint
            return info
        except (httpx.HTTPError, ValueError) as exc:
            info["error"] = f"Ollama is not reachable at {self.base_url} ({type(exc).__name__})"
            info["hint"] = setup_hint(self.base_url, self.model)
            return info
        names = [m["name"] for m in info["models"] if m.get("name")]
        info["model_available"] = self.model_installed(self.model, names)
        if not info["model_available"]:
            info["error"] = f"Model '{self.model}' is not installed in Ollama"
            info["hint"] = f"Run `ollama pull {self.model}` or choose an installed model in Settings."
        return info

    async def ensure_ready(self) -> None:
        status = await self.status()
        if not status["ollama_reachable"] or not status["model_available"]:
            raise LLMUnavailableError(status["error"] or "Model unavailable", status["hint"])

    async def chat(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = 1024,
                   on_progress: ProgressCallback | None = None, temperature: float | None = None) -> LLMResult:
        self._check_local()
        options: dict[str, Any] = {"num_ctx": self.num_ctx, "num_predict": max_tokens, "top_p": 0.9,
                                   "temperature": self.temperature if temperature is None else temperature}
        if self.seed is not None:
            options["seed"] = self.seed
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True,
                                   "keep_alive": "30m", "options": options}
        if schema is not None:
            payload["format"] = schema
        timeout = httpx.Timeout(connect=5.0, read=self.timeout_s, write=60.0, pool=5.0)
        start = time.monotonic()
        parts: list[str] = []
        chunks = 0
        prompt_tokens = completion_tokens = 0
        done_reason = None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                    if resp.status_code == 404:
                        raise LLMUnavailableError(f"Model '{self.model}' is not installed in Ollama",
                                                  f"Run `ollama pull {self.model}`.")
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                        raise LLMResponseError(f"Ollama returned HTTP {resp.status_code}: {body}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise LLMResponseError(f"Ollama error: {chunk['error']}")
                        piece = (chunk.get("message") or {}).get("content") or ""
                        if piece:
                            parts.append(piece)
                            chunks += 1
                            if on_progress is not None and chunks % 16 == 0:
                                await on_progress(chunks)
                        if chunk.get("done"):
                            prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                            completion_tokens = int(chunk.get("eval_count") or chunks)
                            done_reason = chunk.get("done_reason")
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(f"Cannot connect to Ollama at {self.base_url}",
                                      setup_hint(self.base_url, self.model)) from exc
        except httpx.TimeoutException as exc:
            raise LLMResponseError(f"Ollama did not respond within {self.timeout_s:.0f}s") from exc
        return LLMResult(content="".join(parts), prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                         duration_ms=int((time.monotonic() - start) * 1000), model=self.model,
                         truncated=done_reason == "length")

    async def pull(self, model: str, on_update: Callable[[dict], Awaitable[None]]) -> None:
        """Ask the local Ollama daemon to download a model (explicit user action from the setup screen)."""
        self._check_local()
        if not MODEL_NAME_RE.match(model):
            raise ValueError("invalid model name")
        layers: dict[str, tuple[int, int]] = {}
        timeout = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/pull", json={"model": model, "stream": True}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                    raise LLMResponseError(f"Ollama pull failed (HTTP {resp.status_code}): {body}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise LLMResponseError(data["error"])
                    digest = data.get("digest")
                    if digest and data.get("total"):
                        layers[digest] = (int(data.get("completed") or 0), int(data["total"]))
                    completed = sum(c for c, _ in layers.values()) if layers else None
                    total = sum(t for _, t in layers.values()) if layers else None
                    await on_update({"status_text": data.get("status", ""), "completed": completed, "total": total})


async def chat_json(client: LLMClient, messages: list[dict], model_cls: type[T], *, max_tokens: int,
                    on_progress: ProgressCallback | None = None, retries: int = 1) -> tuple[T, list[LLMResult]]:
    """Structured output with schema-constrained decoding, validation and one repair retry."""
    schema = inline_refs(model_cls.model_json_schema())
    results: list[LLMResult] = []
    convo = list(messages)
    last_error = ""
    for _attempt in range(retries + 1):
        result = await client.chat(convo, schema=schema, max_tokens=max_tokens, on_progress=on_progress)
        results.append(result)
        try:
            data = extract_json(result.content)
            return model_cls.model_validate(data), results
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = str(exc)[:600]
            if result.truncated:
                last_error = "output was cut off at the token limit; be more concise. " + last_error
            convo = list(messages) + [
                {"role": "assistant", "content": result.content[:4000]},
                {"role": "user", "content": f"That response was not valid JSON for the schema ({last_error}). "
                                            "Reply again with ONLY valid JSON matching the schema, more concisely."},
            ]
        await asyncio.sleep(0)
    raise LLMResponseError(f"Model did not produce valid structured output: {last_error}")
