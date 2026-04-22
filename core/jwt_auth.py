"""
core/jwt_auth.py — Inbound JWT validation against an OIDC issuer's JWKS.

Used by core.auth.MCPAuthMiddleware when MCP_AUTH_MODE is "jwt" or "both".
Caches the JWKS in-memory with a TTL so the IdP is not hit on every request.
Validates signature, issuer, audience, and expiry against the configured
OIDC_* settings, then maps the resulting claims onto a core.identity.Identity.

The role claim may be a plain string or a list (Auth0 / Keycloak / Cognito
all behave differently). When it's a list, the highest-privilege role from
the project's RBAC hierarchy wins.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Highest → lowest. Mirrors core.rbac.ROLE_HIERARCHY ordering.
_ROLE_PRIORITY = ("admin", "manager", "supervisor", "technician", "readonly")


class JWTValidationError(Exception):
    """Raised when a JWT cannot be validated."""


class _JWKSCache:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._jwks: Optional[Dict[str, Any]] = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, url: str, force_refresh: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        if not force_refresh and self._jwks is not None and now - self._fetched_at < self._ttl:
            return self._jwks
        async with self._lock:
            now = time.monotonic()
            if not force_refresh and self._jwks is not None and now - self._fetched_at < self._ttl:
                return self._jwks
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                self._jwks = resp.json()
                self._fetched_at = now
                return self._jwks


_jwks_cache: Optional[_JWKSCache] = None


def _cache() -> _JWKSCache:
    global _jwks_cache
    if _jwks_cache is None:
        from core.settings import get_settings
        _jwks_cache = _JWKSCache(ttl_seconds=get_settings().OIDC_JWKS_CACHE_TTL_SECONDS)
    return _jwks_cache


def _resolve_jwks_url(issuer: str, jwks_url_setting: Optional[str]) -> str:
    if jwks_url_setting:
        return jwks_url_setting
    return issuer.rstrip("/") + "/.well-known/jwks.json"


async def validate_jwt(token: str) -> Dict[str, Any]:
    """
    Validate a JWT and return its claims dict. Raises JWTValidationError on
    any failure path (malformed header, missing kid, JWKS miss, bad sig,
    wrong iss/aud, expired).
    """
    try:
        import jwt
        from jwt.algorithms import RSAAlgorithm
    except ImportError as exc:
        raise JWTValidationError(
            "PyJWT with cryptography extras is required for MCP_AUTH_MODE=jwt. "
            "Install with: pip install 'pyjwt[crypto]>=2.8.0'"
        ) from exc

    from core.settings import get_settings
    s = get_settings()

    if not (s.OIDC_ISSUER and s.OIDC_AUDIENCE):
        raise JWTValidationError(
            "OIDC_ISSUER and OIDC_AUDIENCE must be set for JWT validation"
        )

    jwks_url = _resolve_jwks_url(s.OIDC_ISSUER, s.OIDC_JWKS_URL)

    try:
        unverified_header = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001
        raise JWTValidationError(f"Malformed JWT header: {exc}") from exc

    kid = unverified_header.get("kid")
    if not kid:
        raise JWTValidationError("JWT missing 'kid' header")

    jwks = await _cache().get(jwks_url)
    matching = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if matching is None:
        # Key may have rotated since the last cache fill. Force-refresh once.
        jwks = await _cache().get(jwks_url, force_refresh=True)
        matching = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if matching is None:
            raise JWTValidationError(f"No JWKS key matches kid={kid}")

    try:
        public_key = RSAAlgorithm.from_jwk(matching)
    except Exception as exc:  # noqa: BLE001
        raise JWTValidationError(f"Failed to load public key from JWKS: {exc}") from exc

    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=list(s.OIDC_ALGORITHMS),
            audience=s.OIDC_AUDIENCE,
            issuer=s.OIDC_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise JWTValidationError("JWT expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise JWTValidationError(f"Invalid audience: {exc}") from exc
    except jwt.InvalidIssuerError as exc:
        raise JWTValidationError(f"Invalid issuer: {exc}") from exc
    except jwt.InvalidTokenError as exc:
        raise JWTValidationError(f"Invalid token: {exc}") from exc

    return claims


def claims_to_identity(claims: Dict[str, Any]):
    """Map validated JWT claims onto an Identity."""
    from core.identity import Identity
    from core.settings import get_settings
    s = get_settings()

    user_id = str(claims.get(s.OIDC_USER_CLAIM) or claims.get("sub") or "anonymous")
    tenant_id = str(claims.get(s.OIDC_TENANT_CLAIM) or "default")

    role_raw = claims.get(s.OIDC_ROLE_CLAIM)
    if isinstance(role_raw, list):
        role = next((r for r in _ROLE_PRIORITY if r in role_raw), "readonly")
    elif isinstance(role_raw, str) and role_raw:
        role = role_raw
    else:
        role = "readonly"

    return Identity(user_id=user_id, role=role, tenant_id=tenant_id)
