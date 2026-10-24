"""Local-only HTTP hardening.

Defends the localhost API against DNS rebinding (Host allowlist), cross-site
requests (Origin allowlist + mandatory custom header on state-changing methods)
and other local processes (optional per-launch bearer token).
"""

from __future__ import annotations

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PUBLIC_PATHS = {"/api/health"}
CLIENT_HEADER = "x-locpilot-client"


def _host_without_port(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class LocalSecurityMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_origins: list[str], auth_token: str | None) -> None:
        self.app = app
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins}
        self.auth_token = auth_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        host = _host_without_port(headers.get("host", ""))
        if host not in _ALLOWED_HOSTS:
            await self._reject(send, 403, "host_not_allowed", "Requests must target 127.0.0.1 or localhost.")
            return

        origin = headers.get("origin")
        if origin is not None and origin.rstrip("/").lower() not in self.allowed_origins:
            await self._reject(send, 403, "origin_not_allowed", f"Origin {origin!r} is not allowed.")
            return

        if method == "OPTIONS":
            # CORS preflight is answered by CORSMiddleware (outermost); never reaches handlers.
            await self.app(scope, receive, send)
            return

        if method not in _SAFE_METHODS and not headers.get(CLIENT_HEADER):
            await self._reject(send, 403, "missing_client_header", "State-changing requests require X-LocPilot-Client.")
            return

        if self.auth_token and path.startswith("/api") and path not in _PUBLIC_PATHS:
            supplied = headers.get("authorization", "")
            expected = f"Bearer {self.auth_token}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                await self._reject(send, 401, "unauthorized", "Missing or invalid LocPilot session token.")
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, status: int, error: str, detail: str) -> None:
        body = json.dumps({"error": error, "detail": detail, "hint": None}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
