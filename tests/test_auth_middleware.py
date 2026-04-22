"""
tests/test_auth_middleware.py — Coverage for the inbound MCPAuthMiddleware
(G6, ASGI layer).

Pins the contract that gates every HTTP / SSE / WebSocket request:
  - exempt paths (e.g. /healthz) bypass auth
  - static-token mode rejects bad tokens
  - JWT mode invokes the validator and rejects failures
  - 'both' mode tries JWT first, falls back to static
  - identity contextvar is bound during downstream call AND reset after
  - websocket failures use close code 1008 (policy violation)

We drive the middleware as raw ASGI — no Starlette TestClient or httpx —
so the tests run in environments without those dependencies.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Optional

# core.auth -> core.jwt_auth -> import httpx; stub it (we never hit the network).
if "httpx" not in sys.modules:
    sys.modules["httpx"] = ModuleType("httpx")

import pytest  # noqa: E402

from core.auth import MCPAuthMiddleware  # noqa: E402
from core.identity import Identity, current_identity  # noqa: E402


# ── ASGI test harness ───────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


class _CapturingApp:
    """A minimal ASGI app that records what scope reaches it and what
    identity is bound at that moment. Optionally emits a 200 response."""

    def __init__(self, emit_response: bool = True):
        self.calls: List[Dict[str, Any]] = []
        self.emit_response = emit_response

    async def __call__(self, scope, receive, send):
        self.calls.append({
            "scope": scope,
            "identity": current_identity(),
        })
        if self.emit_response and scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({"type": "http.response.body", "body": b"ok"})


class _Sink:
    """Captures ASGI messages emitted via `send`."""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    async def send(self, message):
        self.messages.append(message)

    @property
    def status(self) -> Optional[int]:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages
            if m.get("type") == "http.response.body"
        )

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))

    def ws_close_code(self) -> Optional[int]:
        for m in self.messages:
            if m.get("type") == "websocket.close":
                return m.get("code")
        return None


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _http_scope(path: str = "/v1/tools", headers: Optional[Dict[str, str]] = None):
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": raw_headers,
        "query_string": b"",
    }


def _ws_scope(path: str = "/sse", headers: Optional[Dict[str, str]] = None):
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    return {
        "type": "websocket",
        "path": path,
        "headers": raw_headers,
    }


# ── Constructor validation ──────────────────────────────────────────────────

class TestConstructor:
    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="Unknown MCP_AUTH_MODE"):
            MCPAuthMiddleware(_CapturingApp(), mode="bogus",
                              static_token="t", default_role="readonly")

    def test_static_mode_requires_token(self):
        with pytest.raises(RuntimeError, match="MCP_ACCESS_TOKEN is required"):
            MCPAuthMiddleware(_CapturingApp(), mode="static",
                              static_token=None, default_role="readonly")

    def test_both_mode_requires_token(self):
        with pytest.raises(RuntimeError, match="MCP_ACCESS_TOKEN is required"):
            MCPAuthMiddleware(_CapturingApp(), mode="both",
                              static_token=None, default_role="readonly")

    def test_jwt_mode_does_not_require_static_token(self):
        # Should construct cleanly — JWT mode has no static fallback.
        MCPAuthMiddleware(_CapturingApp(), mode="jwt",
                          static_token=None, default_role="readonly")


# ── Non-HTTP scope passthrough ──────────────────────────────────────────────

class TestScopePassthrough:
    def test_lifespan_passes_through(self):
        app = _CapturingApp(emit_response=False)
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="t", default_role="readonly")
        sink = _Sink()
        _run(mw({"type": "lifespan"}, _empty_receive, sink.send))
        assert len(app.calls) == 1
        assert app.calls[0]["scope"]["type"] == "lifespan"


# ── Exempt paths ────────────────────────────────────────────────────────────

class TestExemptPaths:
    def test_healthz_bypasses_auth(self):
        app = _CapturingApp()
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="t", default_role="readonly")
        sink = _Sink()
        _run(mw(_http_scope("/healthz"), _empty_receive, sink.send))
        assert sink.status == 200
        assert len(app.calls) == 1
        # Identity must NOT be bound for exempt paths — they're public.
        assert app.calls[0]["identity"] is None

    def test_healthz_subpath_bypasses_auth(self):
        app = _CapturingApp()
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="t", default_role="readonly")
        sink = _Sink()
        _run(mw(_http_scope("/healthz/details"), _empty_receive, sink.send))
        assert sink.status == 200

    def test_path_starting_with_healthz_substring_not_exempt(self):
        """`/healthznot` must not be exempt — only exact-or-prefix-with-slash."""
        app = _CapturingApp()
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="t", default_role="readonly")
        sink = _Sink()
        _run(mw(_http_scope("/healthznot"), _empty_receive, sink.send))
        assert sink.status == 401


# ── Static mode ─────────────────────────────────────────────────────────────

class TestStaticMode:
    def _mw(self, app):
        return MCPAuthMiddleware(app, mode="static",
                                 static_token="secret-token",
                                 default_role="readonly")

    def test_missing_authorization_rejected(self):
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(_http_scope(), _empty_receive, sink.send))
        assert sink.status == 401
        assert "Bearer" in sink.json()["detail"]
        assert app.calls == []

    def test_wrong_scheme_rejected(self):
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(
            _http_scope(headers={"authorization": "Basic abcd"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 401

    def test_wrong_token_rejected(self):
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(
            _http_scope(headers={"authorization": "Bearer wrong"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 401
        assert "mismatch" in sink.json()["detail"]

    def test_correct_token_admits_request(self):
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(
            _http_scope(headers={"authorization": "Bearer secret-token"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 200
        assert len(app.calls) == 1
        ident = app.calls[0]["identity"]
        assert isinstance(ident, Identity)
        assert ident.user_id == "static"
        assert ident.role == "readonly"
        assert ident.tenant_id == "default"

    def test_x_mcp_headers_override_defaults(self):
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(
            _http_scope(headers={
                "authorization": "Bearer secret-token",
                "x-mcp-user-id": "alice",
                "x-mcp-role": "manager",
                "x-mcp-tenant-id": "acme",
            }),
            _empty_receive, sink.send,
        ))
        assert sink.status == 200
        ident = app.calls[0]["identity"]
        assert ident.user_id == "alice"
        assert ident.role == "manager"
        assert ident.tenant_id == "acme"

    def test_401_response_includes_www_authenticate(self):
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(_http_scope(), _empty_receive, sink.send))
        start = next(m for m in sink.messages if m["type"] == "http.response.start")
        header_names = [name.lower() for name, _ in start["headers"]]
        assert b"www-authenticate" in header_names


# ── JWT mode ────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_jwt_module(monkeypatch):
    """Stub core.jwt_auth so the middleware's lazy import gets a controllable
    validator. Caller toggles `state.fail` to flip success/failure."""
    state = SimpleNamespace(fail=False, claims={"sub": "jwt-user", "role": "manager",
                                                "tenant": "jwt-tenant"},
                             validate_calls=0)

    class JWTValidationError(Exception):
        pass

    async def validate_jwt(token):
        state.validate_calls += 1
        if state.fail:
            raise JWTValidationError("simulated")
        return state.claims

    def claims_to_identity(claims):
        return Identity(
            user_id=str(claims.get("sub", "anonymous")),
            role=str(claims.get("role", "readonly")),
            tenant_id=str(claims.get("tenant", "default")),
        )

    fake = ModuleType("core.jwt_auth")
    fake.JWTValidationError = JWTValidationError  # type: ignore[attr-defined]
    fake.validate_jwt = validate_jwt              # type: ignore[attr-defined]
    fake.claims_to_identity = claims_to_identity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.jwt_auth", fake)
    return state


class TestJwtMode:
    def test_valid_jwt_admits_request(self, fake_jwt_module):
        app = _CapturingApp()
        mw = MCPAuthMiddleware(app, mode="jwt",
                               static_token=None, default_role="readonly")
        sink = _Sink()
        _run(mw(
            _http_scope(headers={"authorization": "Bearer the.jwt.token"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 200
        ident = app.calls[0]["identity"]
        assert ident.user_id == "jwt-user"
        assert ident.role == "manager"
        assert ident.tenant_id == "jwt-tenant"
        assert fake_jwt_module.validate_calls == 1

    def test_invalid_jwt_rejected_no_static_fallback(self, fake_jwt_module):
        fake_jwt_module.fail = True
        app = _CapturingApp()
        mw = MCPAuthMiddleware(app, mode="jwt",
                               static_token=None, default_role="readonly")
        sink = _Sink()
        _run(mw(
            _http_scope(headers={"authorization": "Bearer bad.jwt"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 401
        assert "JWT" in sink.json()["detail"]
        assert app.calls == []

    def test_missing_authorization_rejected(self, fake_jwt_module):
        app = _CapturingApp()
        mw = MCPAuthMiddleware(app, mode="jwt",
                               static_token=None, default_role="readonly")
        sink = _Sink()
        _run(mw(_http_scope(), _empty_receive, sink.send))
        assert sink.status == 401
        # Should not have called the validator at all.
        assert fake_jwt_module.validate_calls == 0


# ── 'both' mode ─────────────────────────────────────────────────────────────

class TestBothMode:
    def _mw(self, app):
        return MCPAuthMiddleware(app, mode="both",
                                 static_token="static-token",
                                 default_role="readonly")

    def test_jwt_success_takes_precedence(self, fake_jwt_module):
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(
            _http_scope(headers={"authorization": "Bearer the.jwt.token"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 200
        ident = app.calls[0]["identity"]
        assert ident.user_id == "jwt-user"

    def test_jwt_failure_falls_back_to_static_match(self, fake_jwt_module):
        fake_jwt_module.fail = True
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(
            _http_scope(headers={"authorization": "Bearer static-token",
                                 "x-mcp-user-id": "ops"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 200
        # JWT was attempted before static fallback.
        assert fake_jwt_module.validate_calls == 1
        ident = app.calls[0]["identity"]
        assert ident.user_id == "ops"

    def test_jwt_failure_and_static_mismatch_rejected(self, fake_jwt_module):
        fake_jwt_module.fail = True
        app = _CapturingApp()
        sink = _Sink()
        _run(self._mw(app)(
            _http_scope(headers={"authorization": "Bearer some-other-token"}),
            _empty_receive, sink.send,
        ))
        assert sink.status == 401
        assert app.calls == []


# ── WebSocket failure semantics ─────────────────────────────────────────────

class TestWebsocket:
    def test_missing_auth_closes_with_policy_violation(self):
        app = _CapturingApp(emit_response=False)
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="t", default_role="readonly")
        sink = _Sink()
        _run(mw(_ws_scope(), _empty_receive, sink.send))
        assert sink.ws_close_code() == 1008
        assert app.calls == []

    def test_valid_auth_passes_to_app(self):
        app = _CapturingApp(emit_response=False)
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="t", default_role="readonly")
        sink = _Sink()
        _run(mw(
            _ws_scope(headers={"authorization": "Bearer t"}),
            _empty_receive, sink.send,
        ))
        # No close emitted — the app got the connection.
        assert sink.ws_close_code() is None
        assert len(app.calls) == 1


# ── Identity contextvar lifecycle ───────────────────────────────────────────

class TestContextvarLifecycle:
    def test_identity_reset_after_request(self):
        """Outside the downstream call, current_identity() must be None even
        right after a successful request — otherwise identity leaks across
        requests on the same task."""
        app = _CapturingApp()
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="secret", default_role="readonly")
        sink = _Sink()

        async def _drive():
            await mw(
                _http_scope(headers={"authorization": "Bearer secret"}),
                _empty_receive, sink.send,
            )
            return current_identity()

        post = _run(_drive())
        assert sink.status == 200
        # Bound at downstream call time.
        assert app.calls[0]["identity"] is not None
        # Reset by the time the middleware returns.
        assert post is None

    def test_identity_reset_even_when_app_raises(self):
        class _Raiser:
            calls = []

            async def __call__(self, scope, receive, send):
                _Raiser.calls.append(current_identity())
                raise RuntimeError("downstream boom")

        app = _Raiser()
        mw = MCPAuthMiddleware(app, mode="static",
                               static_token="secret", default_role="readonly")

        async def _drive():
            try:
                await mw(
                    _http_scope(headers={"authorization": "Bearer secret"}),
                    _empty_receive, _Sink().send,
                )
            except RuntimeError:
                pass
            return current_identity()

        post = _run(_drive())
        # App saw an identity, but it was reset on the way out.
        assert _Raiser.calls and _Raiser.calls[0] is not None
        assert post is None
