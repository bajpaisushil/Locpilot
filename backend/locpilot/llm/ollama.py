"""Ollama HTTP client.

LocPilot talks to a *local* Ollama daemon only. Non-loopback URLs are refused
unless ``LOCPILOT_ALLOW_REMOTE_OLLAMA=true`` is set explicitly, and there is
no fallback to any hosted model API: if Ollama or the model is missing, calls
fail with :class:`LLMUnavailableError` and the UI shows setup instructions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from locpilot.config import ollama_url_is_local

log = logging.getLogger("locpilot.ollama")
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
class ModelUsage:
    """Models this LocPilot run made Ollama load into memory, so it can free them again (shared by clients)."""

    loaded: set[str] = field(default_factory=set)  # loaded because of LocPilot: LocPilot frees them
    checked: set[str] = field(default_factory=set)  # models whose prior state has been looked at

    def forget(self, model: str) -> None:
        self.loaded.discard(model)
        self.checked.discard(model)


@dataclass
class LLMResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    model: str
    truncated: bool = False
    thinking: str = ""  # the model's reasoning before the answer (thinking models with think=True)


class LLMClient(Protocol):
    model: str

    async def chat(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = 1024,
                   on_progress: ProgressCallback | None = None, temperature: float | None = None,
                   think: bool = False) -> LLMResult: ...


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


def _windows_memory() -> tuple[int, int] | None:
    """(installed, available) bytes on Windows."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None
    return status.ullTotalPhys, status.ullAvailPhys


def physical_memory() -> int | None:
    """Installed RAM in bytes, or None where it can't be read."""
    if sys.platform == "win32":
        try:
            found = _windows_memory()
        except (OSError, AttributeError, ValueError):
            return None
        return found[0] if found else None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def available_memory() -> int | None:
    """RAM that can be used without pushing other programs into swap, in bytes (None if unknown).

    Linux: MemAvailable. macOS: free + inactive + speculative + purgeable pages (what the system can hand out
    without swapping). Windows: available physical memory.
    """
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
            return None
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
            page = re.search(r"page size of (\d+) bytes", out)
            pages = dict(re.findall(r"^Pages (free|inactive|speculative|purgeable):\s+(\d+)", out, re.MULTILINE))
            if not page or not pages:
                return None
            return int(page.group(1)) * sum(int(v) for v in pages.values())
        if sys.platform == "win32":
            found = _windows_memory()
            return found[1] if found else None
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None
    return None


# Bytes per cached value for Ollama's KV-cache types (q8_0/q4_0 store blocks of 32 values plus a scale).
KV_BYTES_PER_VALUE = {"f16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}
OVERHEAD_BYTES = 600 * 2**20  # compute buffers and runtime besides the weights and the cache


def kv_cache_bytes(model_info: dict, num_ctx: int, kv_cache_type: str = "f16") -> int | None:
    """Memory the context of ``num_ctx`` tokens takes, from the model's architecture (``/api/show`` model_info).

    An upper bound for models that only attend to a window in some layers. None when the architecture is unknown.
    """
    arch = model_info.get("general.architecture")
    if not arch:
        return None

    def num(key: str) -> float | None:
        value = model_info.get(f"{arch}.{key}")
        if isinstance(value, list):  # per-layer values
            value = max((v for v in value if isinstance(v, int | float)), default=None)
        return float(value) if isinstance(value, int | float) and value > 0 else None

    layers, heads = num("block_count"), num("attention.head_count")
    heads_kv = num("attention.head_count_kv") or heads
    width = num("embedding_length")
    key_len = num("attention.key_length") or (width / heads if width and heads else None)
    value_len = num("attention.value_length") or key_len
    if not (layers and heads_kv and key_len and value_len):
        return None
    per_value = KV_BYTES_PER_VALUE.get(kv_cache_type or "f16", 2.0)
    return int(layers * heads_kv * (key_len + value_len) * num_ctx * per_value)


def pick_installed_model(models: list[dict], ram_bytes: int | None) -> str | None:
    """The installed chat model to use when the configured one isn't installed.

    Embedding-only models are skipped. Prefers the largest model that fits comfortably in memory (file size at most
    half the RAM, leaving room for the context and other apps), else the smallest one; without a RAM figure, the most
    recently downloaded one.
    """
    def chats(m: dict) -> bool:
        caps = m.get("capabilities")
        if caps is not None:
            return "completion" in caps
        return "embed" not in str(m.get("name")).lower()  # older Ollama doesn't report capabilities

    candidates = [m for m in models if m.get("name") and chats(m)]
    if not candidates:
        return None
    limit = ram_bytes / 2 if ram_bytes else None

    def rank(m: dict) -> tuple:
        size = m.get("size_bytes") or 0
        if limit is None:
            return (0, 0, m.get("modified_at") or "")
        fits = size <= limit
        return (fits, size if fits else -size, m.get("modified_at") or "")

    return max(candidates, key=rank)["name"]


def reasoning_in_circles(text: str, window: int = 300) -> bool:
    """True when the reasoning keeps repeating itself: its last ``window`` characters already occurred twice before.

    Small thinking models sometimes loop over the same few sentences until their budget runs out; more of that
    never produces an answer. Quoting the same code once more doesn't count, a third repetition does.
    """
    if len(text) < 3 * window:
        return False
    return text.count(text[-window:]) >= 3


# Model capabilities ("completion", "thinking", "tools", …) per (Ollama URL, model), read once per run.
_show_cache: dict[tuple[str, str], dict] = {}  # /api/show per (Ollama URL, model)


def setup_hint(base_url: str, model: str) -> str:
    return (f"Start Ollama (open the Ollama app or run `ollama serve`) so it listens on {base_url}, "
            f"then download the model with `ollama pull {model}`.")


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, num_ctx: int = 16384, temperature: float = 0.1,
                 timeout_s: float = 900.0, seed: int | None = 7, allow_remote: bool = False,
                 keep_alive: str = "30m", usage: ModelUsage | None = None, thinking_tokens: int = 4096,
                 kv_cache_type: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.seed = seed
        self.allow_remote = allow_remote
        self.keep_alive = keep_alive  # how long Ollama keeps the model in RAM after the last request
        self.usage = usage
        self.thinking_tokens = thinking_tokens  # extra output budget for the reasoning when think=True
        # How Ollama stores the context: set when LocPilot started Ollama with memory_saving_env(), else unknown
        # ("": estimates assume Ollama's 16-bit default, so they err on the safe side).
        self.kv_cache_type = kv_cache_type

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
                               "quantization": details.get("quantization_level"), "family": details.get("family"),
                               "capabilities": m.get("capabilities"), "modified_at": m.get("modified_at")})
            return models

    @staticmethod
    def model_installed(model: str, names: list[str]) -> bool:
        candidates = {model, model if ":" in model else f"{model}:latest"}
        return any(n in candidates for n in names)

    async def _show(self) -> dict | None:
        """Ollama's description of the model (``/api/show``), cached; None on errors."""
        key = (self.base_url, self.model)
        if key in _show_cache:
            return _show_cache[key]
        self._check_local()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.post(f"{self.base_url}/api/show", json={"model": self.model})
                resp.raise_for_status()
                shown = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.info("could not read the details of %s: %s", self.model, exc)
            return None
        if not isinstance(shown, dict):
            return None
        _show_cache[key] = shown
        return shown

    async def capabilities(self) -> frozenset[str] | None:
        """What the model can do (e.g. "thinking"), or None when Ollama doesn't say (older versions, errors)."""
        shown = await self._show()
        caps = shown.get("capabilities") if shown else None
        if not isinstance(caps, list):
            return None
        return frozenset(str(c) for c in caps)

    async def loaded_details(self) -> list[dict]:
        """Models Ollama holds in memory, with the memory each one takes (weights + context)."""
        self._check_local()
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            resp = await client.get(f"{self.base_url}/api/ps")
            resp.raise_for_status()
            return [{"name": m.get("name") or m.get("model"), "size_bytes": m.get("size"),
                     "vram_bytes": m.get("size_vram"), "context_length": m.get("context_length")}
                    for m in resp.json().get("models", []) if m.get("name") or m.get("model")]

    async def memory_check(self) -> dict:
        """How the configured model fits in memory right now.

        ``needed_bytes`` is what loading it would take (weights + context + overhead), 0 when it is already loaded;
        ``short`` is True when that is more than the RAM available, i.e. the machine would start swapping.
        """
        info: dict[str, Any] = {"total_bytes": physical_memory(), "available_bytes": available_memory(),
                                "loaded_bytes": None, "needed_bytes": None, "context_bytes": None,
                                "kv_cache_type": self.kv_cache_type or "f16", "short": False}
        try:
            loaded = await self.loaded_details()
        except (httpx.HTTPError, ValueError):
            loaded = []
        mine = next((m for m in loaded if self.model_installed(self.model, [m["name"]])), None)
        shown = await self._show()
        if shown and isinstance(shown.get("model_info"), dict):
            info["context_bytes"] = kv_cache_bytes(shown["model_info"], self.num_ctx, info["kv_cache_type"])
        if mine is not None:
            info["loaded_bytes"] = mine["size_bytes"]
            info["needed_bytes"] = 0
            return info
        try:
            size = next((m["size_bytes"] for m in await self.list_models()
                         if self.model_installed(self.model, [m["name"]])), None)
        except (httpx.HTTPError, ValueError):
            size = None
        if size:
            context = info["context_bytes"] if info["context_bytes"] is not None else int(self.num_ctx / 16384 * 2**30)
            info["needed_bytes"] = int(size) + context + OVERHEAD_BYTES
            avail = info["available_bytes"]
            info["short"] = avail is not None and info["needed_bytes"] > avail
        return info

    async def loaded_models(self) -> list[str]:
        """Models Ollama currently holds in memory."""
        self._check_local()
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            resp = await client.get(f"{self.base_url}/api/ps")
            resp.raise_for_status()
            return [m.get("name") or m.get("model") for m in resp.json().get("models", []) if m.get("name") or m.get("model")]

    async def unload_models(self, models: Iterable[str]) -> list[str]:
        """Free the memory of those ``models`` that Ollama has loaded. Never loads a model to do so.

        Returns the models unloaded. Errors are logged, not raised: this runs on shutdown and model switches.
        """
        wanted = [m for m in dict.fromkeys(models) if m]
        if not wanted:
            return []
        try:
            loaded = await self.loaded_models()
        except (httpx.HTTPError, LLMUnavailableError, ValueError) as exc:
            log.info("could not list loaded models: %s", exc)
            return []
        unloaded = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            for model in wanted:
                if not self.model_installed(model, loaded):
                    continue
                try:
                    resp = await client.post(f"{self.base_url}/api/generate", json={"model": model, "keep_alive": 0})
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    log.info("could not unload %s: %s", model, exc)
                    continue
                log.info("unloaded %s from memory", model)
                unloaded.append(model)
        return unloaded

    async def status(self) -> dict:
        info: dict[str, Any] = {"ollama_reachable": False, "ollama_version": None, "ollama_url": self.base_url,
                                "configured_model": self.model, "model_available": False, "models": [],
                                "num_ctx": self.num_ctx, "temperature": self.temperature, "error": None, "hint": None,
                                "memory": None}
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
        if info["model_available"]:
            info["memory"] = await self.memory_check()
        if not info["model_available"]:
            info["error"] = f"Model '{self.model}' is not installed in Ollama"
            info["hint"] = f"Run `ollama pull {self.model}` or choose an installed model in Settings."
        return info

    async def ensure_ready(self) -> None:
        status = await self.status()
        if not status["ollama_reachable"] or not status["model_available"]:
            raise LLMUnavailableError(status["error"] or "Model unavailable", status["hint"])

    async def _note_usage(self) -> None:
        """Before the first request for a model: if Ollama doesn't hold it yet, this request loads it for us."""
        usage = self.usage
        if usage is None or self.model in usage.checked:
            return
        usage.checked.add(self.model)
        try:
            already = self.model_installed(self.model, await self.loaded_models())
        except (httpx.HTTPError, ValueError):
            already = False
        if not already:  # a model another program loaded is left to that program
            usage.loaded.add(self.model)

    async def chat(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = 1024,
                   on_progress: ProgressCallback | None = None, temperature: float | None = None,
                   think: bool = False) -> LLMResult:
        """One model call. ``think`` lets a thinking model reason first; for other models it is ignored.

        Thinking models think by default, so they get an explicit ``think`` either way. The reasoning gets its own
        ``thinking_tokens`` budget on top of the answer's. If the model spends all of it, or goes in circles,
        without answering, the reasoning is cut off and the call is repeated once without thinking.
        """
        self._check_local()
        await self._note_usage()
        caps = await self.capabilities()
        if caps is None or "thinking" not in caps:
            return await self._chat_once(messages, schema, max_tokens, on_progress, temperature, think=None)
        if not think:
            return await self._chat_once(messages, schema, max_tokens, on_progress, temperature, think=False)
        result = await self._chat_once(messages, schema, max_tokens + self.thinking_tokens, on_progress, temperature,
                                       think=True, thinking_budget=self.thinking_tokens)
        if result.content.strip() or not result.truncated:
            return result
        log.info("%s gave no answer after reasoning; asking again without thinking", self.model)
        retry = await self._chat_once(messages, schema, max_tokens, on_progress, temperature, think=False)
        return LLMResult(content=retry.content, prompt_tokens=result.prompt_tokens + retry.prompt_tokens,
                         completion_tokens=result.completion_tokens + retry.completion_tokens,
                         duration_ms=result.duration_ms + retry.duration_ms, model=self.model,
                         truncated=retry.truncated, thinking=result.thinking)

    async def _chat_once(self, messages: list[dict], schema: dict | None, max_tokens: int,
                         on_progress: ProgressCallback | None, temperature: float | None, *,
                         think: bool | None, thinking_budget: int | None = None) -> LLMResult:
        options: dict[str, Any] = {"num_ctx": self.num_ctx, "num_predict": max_tokens, "top_p": 0.9,
                                   "temperature": self.temperature if temperature is None else temperature}
        if self.seed is not None:
            options["seed"] = self.seed
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True,
                                   "keep_alive": self.keep_alive, "options": options}
        if schema is not None:
            payload["format"] = schema
        if think is not None:
            payload["think"] = think
        timeout = httpx.Timeout(connect=5.0, read=self.timeout_s, write=60.0, pool=5.0)
        start = time.monotonic()
        parts: list[str] = []
        thoughts: list[str] = []
        chunks = thought_chunks = 0  # streamed pieces (about one token each)
        prompt_tokens = completion_tokens = 0
        done_reason = None
        cut_off = ""  # why the reasoning was stopped before any answer
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
                        message = chunk.get("message") or {}
                        piece, thought = message.get("content") or "", message.get("thinking") or ""
                        if piece:
                            parts.append(piece)
                        if thought:
                            thoughts.append(thought)
                        if piece or thought:  # reasoning is progress too: the UI shows the model is working
                            chunks += 1
                            if on_progress is not None and chunks % 16 == 0:
                                await on_progress(chunks)
                        if thought and thinking_budget is not None and not parts:
                            thought_chunks += 1
                            if thought_chunks >= thinking_budget:
                                cut_off = f"used its whole reasoning budget ({thinking_budget} tokens)"
                            elif thought_chunks % 16 == 0 and reasoning_in_circles("".join(thoughts)):
                                cut_off = f"was reasoning in circles after {thought_chunks} tokens"
                            if cut_off:
                                break  # closing the stream makes Ollama stop generating
                        if chunk.get("done"):
                            prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                            completion_tokens = int(chunk.get("eval_count") or chunks)
                            done_reason = chunk.get("done_reason")
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(f"Cannot connect to Ollama at {self.base_url}",
                                      setup_hint(self.base_url, self.model)) from exc
        except httpx.TimeoutException as exc:
            raise LLMResponseError(f"Ollama did not respond within {self.timeout_s:.0f}s") from exc
        if cut_off:
            log.info("%s %s without answering", self.model, cut_off)
            completion_tokens = chunks
        return LLMResult(content="".join(parts), prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                         duration_ms=int((time.monotonic() - start) * 1000), model=self.model,
                         truncated=bool(cut_off) or done_reason == "length", thinking="".join(thoughts))

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
                    on_progress: ProgressCallback | None = None, retries: int = 1,
                    think: bool = False) -> tuple[T, list[LLMResult]]:
    """Structured output with schema-constrained decoding, validation and one repair retry."""
    schema = inline_refs(model_cls.model_json_schema())
    results: list[LLMResult] = []
    convo = list(messages)
    last_error = ""
    for _attempt in range(retries + 1):
        result = await client.chat(convo, schema=schema, max_tokens=max_tokens, on_progress=on_progress,
                                   think=think)
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
