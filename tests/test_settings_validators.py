"""
tests/test_settings_validators.py — Coverage for the cross-field validators in
core.settings.Settings. These are the only place where misconfigurations get
caught before the app starts; a regression here means a foot-gun ships:

  G4  — CORS allowlist wildcard + credentials trap
  G5  — MCP_SSL_CERTFILE / MCP_SSL_KEYFILE must be set together
  G6  — MCP_AUTH_MODE jwt/both requires OIDC_ISSUER + OIDC_AUDIENCE
  G14 — MAXIMO_ENV is a top-level field with a default
  G17 — MCP_DISCOVERY_ENABLED defaults True (existing behavior preserved)

Skipped cleanly in environments without pydantic_settings; CI installs it via
requirements.txt.
"""

from __future__ import annotations

import os

import pytest

# Hard skip if the runtime dep isn't present — keeps local dev unblocked.
pytest.importorskip("pydantic_settings")

from core.settings import Settings  # noqa: E402


# ── Fixture: clean baseline kwargs so each test only varies what matters ────

def _base(**overrides):
    """Return the minimum kwargs to construct a valid Settings, then layer
    overrides on top. Avoids interference from .env or shell environment."""
    kwargs = dict(
        MAXIMO_URL="https://x.example.com/maximo/oslc",
        MAXIMO_HOST="https://x.example.com",
        AUTH_MODE="basic",
        MAXIMO_USERNAME="u",
        MAXIMO_PASSWORD="p",
    )
    kwargs.update(overrides)
    return kwargs


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Clear MAXIMO_* and MCP_* / OIDC_* env vars and disable .env loading by
    pointing model_config.env_file at a non-existent path. Otherwise the dev's
    real .env contaminates the kwargs we pass."""
    for k in list(os.environ):
        if k.startswith(("MAXIMO_", "MCP_", "OIDC_", "AUDIT_", "PII_")):
            monkeypatch.delenv(k, raising=False)
    # Settings.model_config["env_file"] = ".env"; redirect to nowhere.
    monkeypatch.setattr(
        Settings, "model_config",
        {**Settings.model_config, "env_file": str(tmp_path / "no_such.env")},
        raising=True,
    )


# ── G4: CORS wildcard + credentials trap ────────────────────────────────────

class TestCorsValidator:
    def test_wildcard_with_credentials_rejected(self):
        with pytest.raises(ValueError, match="cannot contain '\\*'"):
            Settings(**_base(
                MCP_ALLOWED_ORIGINS=["*"],
                MCP_CORS_ALLOW_CREDENTIALS=True,
            ))

    def test_wildcard_without_credentials_allowed(self):
        # Browsers permit "*" only when credentials=False — risky but legal.
        s = Settings(**_base(
            MCP_ALLOWED_ORIGINS=["*"],
            MCP_CORS_ALLOW_CREDENTIALS=False,
        ))
        assert s.MCP_ALLOWED_ORIGINS == ["*"]

    def test_explicit_origins_with_credentials_allowed(self):
        s = Settings(**_base(
            MCP_ALLOWED_ORIGINS=["https://app.example.com"],
            MCP_CORS_ALLOW_CREDENTIALS=True,
        ))
        assert s.MCP_ALLOWED_ORIGINS == ["https://app.example.com"]

    def test_empty_origins_default_safe(self):
        s = Settings(**_base())
        assert s.MCP_ALLOWED_ORIGINS == []
        assert s.MCP_CORS_ALLOW_CREDENTIALS is False


# ── G5: SSL pair both-or-neither ────────────────────────────────────────────

class TestSslPairValidator:
    def test_both_set_ok(self, tmp_path):
        cert = tmp_path / "cert.pem"; cert.touch()
        key = tmp_path / "key.pem"; key.touch()
        s = Settings(**_base(
            MCP_SSL_CERTFILE=str(cert),
            MCP_SSL_KEYFILE=str(key),
        ))
        assert s.MCP_SSL_CERTFILE == str(cert)

    def test_only_certfile_rejected(self, tmp_path):
        cert = tmp_path / "cert.pem"; cert.touch()
        with pytest.raises(ValueError, match="must be set together"):
            Settings(**_base(MCP_SSL_CERTFILE=str(cert)))

    def test_only_keyfile_rejected(self, tmp_path):
        key = tmp_path / "key.pem"; key.touch()
        with pytest.raises(ValueError, match="must be set together"):
            Settings(**_base(MCP_SSL_KEYFILE=str(key)))

    def test_neither_set_ok(self):
        s = Settings(**_base())
        assert s.MCP_SSL_CERTFILE is None
        assert s.MCP_SSL_KEYFILE is None


# ── G6: OIDC requirements when auth mode involves JWT ───────────────────────

class TestOidcRequirementValidator:
    def test_jwt_mode_without_oidc_rejected(self):
        with pytest.raises(ValueError, match="OIDC_ISSUER and OIDC_AUDIENCE"):
            Settings(**_base(MCP_AUTH_MODE="jwt"))

    def test_both_mode_without_oidc_rejected(self):
        with pytest.raises(ValueError, match="OIDC_ISSUER and OIDC_AUDIENCE"):
            Settings(**_base(MCP_AUTH_MODE="both"))

    def test_jwt_mode_missing_audience_rejected(self):
        with pytest.raises(ValueError, match="OIDC_ISSUER and OIDC_AUDIENCE"):
            Settings(**_base(
                MCP_AUTH_MODE="jwt",
                OIDC_ISSUER="https://issuer.example.com",
            ))

    def test_jwt_mode_missing_issuer_rejected(self):
        with pytest.raises(ValueError, match="OIDC_ISSUER and OIDC_AUDIENCE"):
            Settings(**_base(
                MCP_AUTH_MODE="jwt",
                OIDC_AUDIENCE="mcp-api",
            ))

    def test_jwt_mode_with_full_oidc_ok(self):
        s = Settings(**_base(
            MCP_AUTH_MODE="jwt",
            OIDC_ISSUER="https://issuer.example.com",
            OIDC_AUDIENCE="mcp-api",
        ))
        assert s.MCP_AUTH_MODE == "jwt"

    def test_static_mode_works_without_oidc(self):
        s = Settings(**_base(MCP_AUTH_MODE="static"))
        assert s.MCP_AUTH_MODE == "static"


# ── Existing AUTH_MODE validators (sanity, prevent regression) ──────────────

class TestMaximoAuthValidator:
    def test_basic_requires_username_and_password(self):
        with pytest.raises(ValueError, match="MAXIMO_USERNAME and MAXIMO_PASSWORD"):
            kwargs = _base()
            kwargs.pop("MAXIMO_USERNAME")
            Settings(**kwargs)

    def test_apikey_requires_api_key(self):
        with pytest.raises(ValueError, match="MAXIMO_API_KEY is required"):
            kwargs = _base(AUTH_MODE="apikey")
            kwargs.pop("MAXIMO_USERNAME", None)
            kwargs.pop("MAXIMO_PASSWORD", None)
            Settings(**kwargs)

    def test_oauth_requires_token_endpoint_and_client(self):
        with pytest.raises(ValueError, match="OAUTH_TOKEN_URL"):
            kwargs = _base(AUTH_MODE="oauth")
            kwargs.pop("MAXIMO_USERNAME", None)
            kwargs.pop("MAXIMO_PASSWORD", None)
            Settings(**kwargs)


# ── G14 / G17 / Pii / Audit defaults ────────────────────────────────────────

class TestFieldDefaults:
    def test_maximo_env_defaults_to_dev(self):
        # G14 — every audit record needs an env stamp; default keeps existing
        # behavior safe for fresh deployments.
        assert Settings(**_base()).MAXIMO_ENV == "dev"

    def test_discovery_enabled_default_true(self):
        # G17 — backward compatible; operators opt OUT explicitly in prod.
        assert Settings(**_base()).MCP_DISCOVERY_ENABLED is True

    def test_pii_mask_enabled_default_true(self):
        # G3 — masking is on by default; operators opt OUT explicitly.
        assert Settings(**_base()).PII_MASK_ENABLED is True

    def test_pii_mask_fields_default_empty(self):
        assert Settings(**_base()).PII_MASK_FIELDS == []

    def test_pii_mask_value_default(self):
        assert Settings(**_base()).PII_MASK_VALUE == "***MASKED***"

    def test_audit_sink_default_file(self):
        # G8 — backward compatible default.
        assert Settings(**_base()).AUDIT_SINK == "file"

    def test_audit_file_rotation_defaults(self):
        s = Settings(**_base())
        assert s.AUDIT_FILE_MAX_BYTES == 50_000_000
        assert s.AUDIT_FILE_BACKUP_COUNT == 5


class TestAuditSinkLiteral:
    @pytest.mark.parametrize("mode", ["file", "stdout", "both"])
    def test_valid_sink_modes_accepted(self, mode):
        assert Settings(**_base(AUDIT_SINK=mode)).AUDIT_SINK == mode

    def test_unknown_sink_mode_rejected(self):
        with pytest.raises(ValueError):
            Settings(**_base(AUDIT_SINK="kafka"))


class TestAuthModeLiteral:
    @pytest.mark.parametrize("mode", ["static", "jwt", "both"])
    def test_valid_modes_accepted(self, mode):
        kwargs = _base(MCP_AUTH_MODE=mode)
        if mode in ("jwt", "both"):
            kwargs["OIDC_ISSUER"] = "https://x.example.com"
            kwargs["OIDC_AUDIENCE"] = "mcp-api"
        assert Settings(**kwargs).MCP_AUTH_MODE == mode

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            Settings(**_base(MCP_AUTH_MODE="bogus"))


class TestLogLevelValidator:
    @pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
    def test_lowercase_normalized_to_upper(self, level):
        assert Settings(**_base(LOG_LEVEL=level)).LOG_LEVEL == level.upper()

    def test_unknown_level_rejected(self):
        with pytest.raises(ValueError, match="LOG_LEVEL must be one of"):
            Settings(**_base(LOG_LEVEL="trace"))
