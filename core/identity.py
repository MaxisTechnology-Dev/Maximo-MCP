"""
core/identity.py — Per-request caller identity propagated via contextvars.

In HTTP / SSE mode the IdentityMiddleware (see core/web.py) reads identity
headers from the request and stores an Identity on a contextvar. RBAC, the
rate limiter, and the audit logger then read it through resolve_identity().

When no request context is active (stdio mode, background tasks, tests) the
resolver falls back to the process-level CURRENT_USER_ID / CURRENT_USER_ROLE
settings so single-tenant local installs keep working unchanged.

Identity headers consumed (case-insensitive):
    X-MCP-User-Id     caller identity for audit + rate limit (defaults to "anonymous")
    X-MCP-Tenant-Id   tenant scope for rate-limit keying      (defaults to "default")
    X-MCP-Role        RBAC role override                      (defaults to settings.CURRENT_USER_ROLE)

The expectation is that an authenticated edge gateway (Auth0, Cognito,
EntraID, Apigee, NGINX with auth_request, etc.) validates the JWT and
forwards the resolved claims to this service over a trusted internal hop.
The MCP service itself does not yet perform JWT validation — that lands
with G6 in PRODUCT_GAPS_BEFORE_DEPLOY.md.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Identity:
    user_id: str
    role: str
    tenant_id: str

    @property
    def rate_limit_key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"


_identity_var: ContextVar[Optional[Identity]] = ContextVar("mcp_identity", default=None)


def set_identity(identity: Identity) -> Token:
    return _identity_var.set(identity)


def reset_identity(token: Token) -> None:
    _identity_var.reset(token)


def current_identity() -> Optional[Identity]:
    return _identity_var.get()


def resolve_identity() -> Identity:
    """Active per-request identity, or env-based fallback for stdio / tests."""
    ident = _identity_var.get()
    if ident is not None:
        return ident
    from core.settings import get_settings
    s = get_settings()
    return Identity(user_id=s.CURRENT_USER_ID, role=s.CURRENT_USER_ROLE, tenant_id="default")
