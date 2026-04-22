"""
core/auth.py — Inbound and outbound authentication for the MCP service.

Inbound (HTTP / SSE transport)
------------------------------
MCPAuthMiddleware is a pure-ASGI middleware that authenticates every incoming
HTTP / WebSocket request and binds the resulting caller identity to the
per-request contextvar in core.identity. Three modes are supported via
settings.MCP_AUTH_MODE:

    static — Authorization: Bearer <MCP_ACCESS_TOKEN>; identity is taken from
             X-MCP-User-Id / X-MCP-Tenant-Id / X-MCP-Role headers (typically
             injected by an upstream gateway), falling back to env defaults.
    jwt    — Authorization: Bearer <JWT>; the JWT is validated against the
             configured OIDC issuer's JWKS and identity is derived from
             the configured OIDC_*_CLAIM claims.
    both   — JWT is tried first; on validation failure the static path is
             attempted as a fallback. Useful for ops/CI that uses the static
             token while real users use JWTs.

Pure-ASGI (rather than starlette.BaseHTTPMiddleware) so that contextvar
mutations propagate into the downstream app — BaseHTTPMiddleware runs the
downstream call in a separate task, breaking contextvar inheritance.

Outbound (calls to Maximo)
--------------------------
AuthManager owns API key / Basic / OAuth credentials for the Maximo OSLC
client. These are unrelated to the inbound MCPAuthMiddleware above.
"""

import hmac
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Inbound auth middleware ──────────────────────────────────────────────────

class MCPAuthMiddleware:
    """
    Authenticate an HTTP / WebSocket request and bind an Identity to the
    request-scoped contextvar. See module docstring for mode semantics.
    """

    def __init__(
        self,
        app: Any,
        mode: str,
        static_token: Optional[str],
        default_role: str,
        exempt_paths: Tuple[str, ...] = ("/healthz",),
    ) -> None:
        if mode not in ("static", "jwt", "both"):
            raise ValueError(f"Unknown MCP_AUTH_MODE: {mode!r}")
        if mode in ("static", "both") and not static_token:
            raise RuntimeError(
                f"MCP_ACCESS_TOKEN is required when MCP_AUTH_MODE={mode!r}"
            )
        self._app = app
        self._mode = mode
        self._static_expected = f"Bearer {static_token}" if static_token else None
        self._default_role = default_role
        self._exempt_paths = exempt_paths

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path == p or path.startswith(f"{p}/") for p in self._exempt_paths):
            await self._app(scope, receive, send)
            return

        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1", errors="replace")
            for name, value in scope.get("headers", [])
        }
        auth_header = headers.get("authorization", "")

        identity, failure_reason = await self._authenticate(auth_header, headers)

        if identity is None:
            if scope["type"] == "http":
                await self._send_401(send, failure_reason)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return

        from core.identity import reset_identity, set_identity
        token = set_identity(identity)
        try:
            await self._app(scope, receive, send)
        finally:
            reset_identity(token)

    async def _authenticate(
        self,
        auth_header: str,
        headers: Dict[str, str],
    ) -> Tuple[Optional[Any], str]:
        """Returns (Identity, reason). Identity is None on failure."""
        from core.identity import Identity

        if not auth_header.startswith("Bearer "):
            return None, "Authorization: Bearer header missing"

        token_value = auth_header[len("Bearer "):]

        # 1) JWT path
        if self._mode in ("jwt", "both"):
            from core.jwt_auth import JWTValidationError, claims_to_identity, validate_jwt
            try:
                claims = await validate_jwt(token_value)
                return claims_to_identity(claims), "ok"
            except JWTValidationError as exc:
                if self._mode == "jwt":
                    logger.info("JWT auth rejected: %s", exc)
                    return None, f"JWT validation failed: {exc}"
                # mode == "both" → fall through to static
                logger.debug("JWT failed in 'both' mode, attempting static: %s", exc)

        # 2) Static path
        if self._mode in ("static", "both") and self._static_expected:
            if hmac.compare_digest(auth_header, self._static_expected):
                return Identity(
                    user_id=headers.get("x-mcp-user-id") or "static",
                    role=headers.get("x-mcp-role") or self._default_role,
                    tenant_id=headers.get("x-mcp-tenant-id") or "default",
                ), "ok"
            return None, "Static bearer token mismatch"

        return None, "No auth method matched"

    @staticmethod
    async def _send_401(
        send: Callable[[Any], Awaitable[None]],
        detail: str,
    ) -> None:
        import json
        body = json.dumps({"error": "Unauthorized", "detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="MCP"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


class AuthManager:
    """
    Manages API key validation and OAuth token lifecycle.
    Used by server.py middleware and maximo_client.py.
    """

    def __init__(self):
        from core.settings import get_settings
        self.settings = get_settings()
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0.0

    def validate_api_key(self, provided_key: str) -> bool:
        """
        Validate a provided API key against the configured MAXIMO_API_KEY.
        Returns True if valid or if AUTH_MODE != apikey.
        """
        if self.settings.AUTH_MODE != "apikey":
            return True
        expected = self.settings.MAXIMO_API_KEY
        if not expected:
            logger.error("AUTH_MODE=apikey but MAXIMO_API_KEY is not set")
            return False
        # Constant-time comparison to prevent timing attacks
        return hmac.compare_digest(provided_key.encode(), expected.encode())

    async def get_oauth_token(self) -> str:
        """
        Return a valid OAuth bearer token, refreshing if expired.
        Raises RuntimeError if not in oauth mode.
        """
        if self.settings.AUTH_MODE != "oauth":
            raise RuntimeError("get_oauth_token() called but AUTH_MODE != oauth")

        if self._cached_token and time.time() < self._token_expiry - 30:
            return self._cached_token

        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.settings.OAUTH_TOKEN_URL or "",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.OAUTH_CLIENT_ID,
                    "client_secret": self.settings.OAUTH_CLIENT_SECRET,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            self._cached_token = payload["access_token"]
            self._token_expiry = time.time() + payload.get("expires_in", 3600)
            logger.info("OAuth token refreshed")
            return self._cached_token

    def get_basic_auth_header(self) -> str:
        """Return Base64-encoded Basic Auth header value."""
        import base64
        creds = f"{self.settings.MAXIMO_USERNAME}:{self.settings.MAXIMO_PASSWORD}"
        return base64.b64encode(creds.encode()).decode()


# Singleton
_auth_manager: Optional[AuthManager] = None


def get_auth_manager() -> AuthManager:
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = AuthManager()
    return _auth_manager
