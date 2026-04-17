"""
tests/test_jwt_auth.py — Coverage for inbound JWT validation (G6).

The hosted MCP service trusts whatever Identity falls out of these functions
to drive RBAC, rate-limit keying, and audit attribution. A regression here
escalates to "anyone with any JWT can act as anyone." Pin the contract.

These tests mint short-lived RSA-signed JWTs locally and stub the JWKS cache,
so no real OIDC issuer is contacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from types import ModuleType, SimpleNamespace

# core.jwt_auth imports httpx at module level for the JWKS fetch path. Stub it
# before that import — these tests never hit the network because the JWKS
# cache is monkeypatched on every test.
if "httpx" not in sys.modules:
    sys.modules["httpx"] = ModuleType("httpx")

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rsa_key():
    """Generate one RSA keypair for the whole test module (slow op)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "test-kid-1"
    public_jwk["alg"] = "RS256"
    public_jwk["use"] = "sig"
    return SimpleNamespace(private=private_key, jwk=public_jwk, kid="test-kid-1")


@pytest.fixture
def oidc_settings(monkeypatch):
    """Stub core.settings with OIDC config so jwt_auth's lazy import resolves
    to a configurable namespace (avoids importing pydantic_settings)."""
    state = SimpleNamespace(
        OIDC_ISSUER="https://issuer.example.com",
        OIDC_AUDIENCE="mcp-api",
        OIDC_JWKS_URL=None,
        OIDC_ALGORITHMS=["RS256"],
        OIDC_JWKS_CACHE_TTL_SECONDS=3600,
        OIDC_USER_CLAIM="sub",
        OIDC_ROLE_CLAIM="role",
        OIDC_TENANT_CLAIM="tenant",
        CURRENT_USER_ID="system",
        CURRENT_USER_ROLE="readonly",
    )
    fake = ModuleType("core.settings")
    fake.get_settings = lambda: state  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.settings", fake)
    return state


@pytest.fixture
def jwks_stub(monkeypatch, rsa_key):
    """Replace core.jwt_auth._cache() with a stub whose `keys` and refresh
    behavior the test can poke directly. Returns the stub for inspection."""
    import core.jwt_auth as ja

    state = SimpleNamespace(
        keys=[rsa_key.jwk],   # what the cache currently knows about
        get_calls=0,
        force_refresh_calls=0,
    )

    class _StubCache:
        async def get(self, url, force_refresh=False):
            state.get_calls += 1
            if force_refresh:
                state.force_refresh_calls += 1
            return {"keys": list(state.keys)}

    stub_cache = _StubCache()
    monkeypatch.setattr(ja, "_cache", lambda: stub_cache)
    # Also bust the module-level singleton so a real fetch can never happen.
    monkeypatch.setattr(ja, "_jwks_cache", stub_cache, raising=False)
    return state


def _mint(rsa_key, *, claims_overrides=None, headers_overrides=None,
          issuer="https://issuer.example.com", audience="mcp-api",
          ttl_seconds=300):
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + ttl_seconds,
        "sub": "user-123",
        "role": "technician",
        "tenant": "tenant-a",
    }
    if claims_overrides:
        claims.update(claims_overrides)

    headers = {"kid": rsa_key.kid}
    if headers_overrides:
        headers.update(headers_overrides)

    return pyjwt.encode(claims, rsa_key.private, algorithm="RS256", headers=headers)


def _run(coro):
    """Drive an async coro from a sync test body (no pytest-asyncio)."""
    return asyncio.run(coro)


# ── validate_jwt: happy path ────────────────────────────────────────────────

class TestValidateJwtHappy:
    def test_valid_token_returns_claims(self, oidc_settings, jwks_stub, rsa_key):
        from core.jwt_auth import validate_jwt
        token = _mint(rsa_key)
        claims = _run(validate_jwt(token))
        assert claims["sub"] == "user-123"
        assert claims["aud"] == "mcp-api"
        assert claims["iss"] == "https://issuer.example.com"
        assert claims["role"] == "technician"


# ── validate_jwt: failure paths ─────────────────────────────────────────────

class TestValidateJwtFailures:
    def test_expired_token_rejected(self, oidc_settings, jwks_stub, rsa_key):
        from core.jwt_auth import JWTValidationError, validate_jwt
        token = _mint(rsa_key, ttl_seconds=-60)  # already expired
        with pytest.raises(JWTValidationError, match="expired"):
            _run(validate_jwt(token))

    def test_wrong_audience_rejected(self, oidc_settings, jwks_stub, rsa_key):
        from core.jwt_auth import JWTValidationError, validate_jwt
        token = _mint(rsa_key, audience="other-api")
        with pytest.raises(JWTValidationError, match="audience"):
            _run(validate_jwt(token))

    def test_wrong_issuer_rejected(self, oidc_settings, jwks_stub, rsa_key):
        from core.jwt_auth import JWTValidationError, validate_jwt
        token = _mint(rsa_key, issuer="https://attacker.example.com")
        with pytest.raises(JWTValidationError, match="issuer"):
            _run(validate_jwt(token))

    def test_missing_kid_rejected(self, oidc_settings, jwks_stub, rsa_key):
        from core.jwt_auth import JWTValidationError, validate_jwt
        # Encoding without a kid header.
        token = pyjwt.encode(
            {"iss": "https://issuer.example.com", "aud": "mcp-api",
             "iat": int(time.time()), "exp": int(time.time()) + 60,
             "sub": "u"},
            rsa_key.private, algorithm="RS256",
        )
        with pytest.raises(JWTValidationError, match="kid"):
            _run(validate_jwt(token))

    def test_malformed_token_rejected(self, oidc_settings, jwks_stub):
        from core.jwt_auth import JWTValidationError, validate_jwt
        with pytest.raises(JWTValidationError, match="Malformed"):
            _run(validate_jwt("not.a.jwt"))

    def test_signed_with_unknown_key_rejected(self, oidc_settings, jwks_stub, rsa_key):
        """Token signed with a different RSA key whose kid is not in JWKS."""
        from core.jwt_auth import JWTValidationError, validate_jwt
        rogue_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = pyjwt.encode(
            {"iss": "https://issuer.example.com", "aud": "mcp-api",
             "iat": int(time.time()), "exp": int(time.time()) + 60,
             "sub": "u"},
            rogue_priv, algorithm="RS256", headers={"kid": "rogue-kid"},
        )
        with pytest.raises(JWTValidationError, match="No JWKS key matches"):
            _run(validate_jwt(token))

    def test_kid_present_but_signature_mismatch_rejected(
        self, oidc_settings, jwks_stub, rsa_key
    ):
        """Attacker reuses a known kid but signs with a different key."""
        from core.jwt_auth import JWTValidationError, validate_jwt
        rogue_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = pyjwt.encode(
            {"iss": "https://issuer.example.com", "aud": "mcp-api",
             "iat": int(time.time()), "exp": int(time.time()) + 60,
             "sub": "u"},
            rogue_priv, algorithm="RS256", headers={"kid": rsa_key.kid},
        )
        with pytest.raises(JWTValidationError, match="Invalid token"):
            _run(validate_jwt(token))

    def test_missing_required_claims_rejected(self, oidc_settings, jwks_stub, rsa_key):
        """`options={'require': [...]}` should reject tokens missing iat/iss/aud/exp."""
        from core.jwt_auth import JWTValidationError, validate_jwt
        # Skip iat — PyJWT should refuse on require=["exp","iat","iss","aud"].
        now = int(time.time())
        token = pyjwt.encode(
            {"iss": "https://issuer.example.com", "aud": "mcp-api",
             "exp": now + 60, "sub": "u"},
            rsa_key.private, algorithm="RS256", headers={"kid": rsa_key.kid},
        )
        with pytest.raises(JWTValidationError, match="Invalid token"):
            _run(validate_jwt(token))

    def test_missing_oidc_config_rejected(self, oidc_settings, jwks_stub, rsa_key):
        from core.jwt_auth import JWTValidationError, validate_jwt
        oidc_settings.OIDC_ISSUER = None
        token = _mint(rsa_key)
        with pytest.raises(JWTValidationError, match="OIDC_ISSUER"):
            _run(validate_jwt(token))


# ── validate_jwt: JWKS cache rotation behavior ──────────────────────────────

class TestJwksRotation:
    def test_kid_miss_triggers_force_refresh(
        self, oidc_settings, jwks_stub, rsa_key, monkeypatch
    ):
        """If the cached JWKS doesn't contain the token's kid, the cache is
        force-refreshed once. Simulate a key rotation: cache starts empty, but
        a force refresh returns the matching key."""
        from core.jwt_auth import validate_jwt

        # First call returns empty keys; force-refresh returns the real key.
        call_count = {"n": 0}

        async def _patched_get(url, force_refresh=False):
            jwks_stub.get_calls += 1
            if force_refresh:
                jwks_stub.force_refresh_calls += 1
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"keys": []}
            return {"keys": [rsa_key.jwk]}

        import core.jwt_auth as ja
        monkeypatch.setattr(ja._cache(), "get", _patched_get)

        token = _mint(rsa_key)
        claims = _run(validate_jwt(token))
        assert claims["sub"] == "user-123"
        assert jwks_stub.force_refresh_calls == 1
        assert jwks_stub.get_calls == 2

    def test_kid_miss_after_refresh_still_fails(
        self, oidc_settings, jwks_stub, rsa_key, monkeypatch
    ):
        from core.jwt_auth import JWTValidationError, validate_jwt

        async def _patched_get(url, force_refresh=False):
            jwks_stub.get_calls += 1
            if force_refresh:
                jwks_stub.force_refresh_calls += 1
            return {"keys": []}  # never returns the key

        import core.jwt_auth as ja
        monkeypatch.setattr(ja._cache(), "get", _patched_get)

        token = _mint(rsa_key)
        with pytest.raises(JWTValidationError, match="No JWKS key matches"):
            _run(validate_jwt(token))
        assert jwks_stub.force_refresh_calls == 1


# ── claims_to_identity ──────────────────────────────────────────────────────

class TestClaimsToIdentity:
    def test_basic_mapping(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({"sub": "u1", "role": "manager", "tenant": "t1"})
        assert ident.user_id == "u1"
        assert ident.role == "manager"
        assert ident.tenant_id == "t1"

    def test_role_as_list_picks_highest_priority(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({
            "sub": "u1",
            "role": ["readonly", "admin", "technician"],
            "tenant": "t1",
        })
        assert ident.role == "admin"

    def test_role_list_order_independent(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({
            "sub": "u1",
            "role": ["technician", "readonly", "manager"],
            "tenant": "t1",
        })
        assert ident.role == "manager"

    def test_role_list_unknown_roles_fallback_to_readonly(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({
            "sub": "u1",
            "role": ["custom-role", "another"],
            "tenant": "t1",
        })
        assert ident.role == "readonly"

    def test_role_missing_defaults_to_readonly(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({"sub": "u1", "tenant": "t1"})
        assert ident.role == "readonly"

    def test_role_empty_string_defaults_to_readonly(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({"sub": "u1", "role": "", "tenant": "t1"})
        assert ident.role == "readonly"

    def test_role_string_passthrough(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({"sub": "u1", "role": "custom-role", "tenant": "t1"})
        # A non-hierarchy string is taken as-is — RBAC layer will reject if unknown.
        assert ident.role == "custom-role"

    def test_user_id_falls_back_to_sub(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        oidc_settings.OIDC_USER_CLAIM = "preferred_username"
        ident = claims_to_identity({"sub": "user-uuid"})  # preferred_username absent
        assert ident.user_id == "user-uuid"

    def test_user_id_uses_custom_claim_when_present(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        oidc_settings.OIDC_USER_CLAIM = "preferred_username"
        ident = claims_to_identity({
            "sub": "user-uuid",
            "preferred_username": "alice",
        })
        assert ident.user_id == "alice"

    def test_no_identifying_claim_falls_back_to_anonymous(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({})
        assert ident.user_id == "anonymous"

    def test_tenant_falls_back_to_default(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({"sub": "u1"})
        assert ident.tenant_id == "default"

    def test_custom_role_claim_name(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        oidc_settings.OIDC_ROLE_CLAIM = "groups"
        ident = claims_to_identity({"sub": "u1", "groups": ["admin"]})
        assert ident.role == "admin"

    def test_custom_tenant_claim_name(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        oidc_settings.OIDC_TENANT_CLAIM = "org"
        ident = claims_to_identity({"sub": "u1", "org": "acme"})
        assert ident.tenant_id == "acme"

    def test_user_id_coerced_to_string(self, oidc_settings):
        from core.jwt_auth import claims_to_identity
        ident = claims_to_identity({"sub": 12345})
        assert ident.user_id == "12345"
