"""
core/settings.py — Pydantic BaseSettings for Maximo Enterprise MCP.
Supports both API key and username/password (Basic Auth) modes.
"""

from typing import List, Literal, Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator, model_validator


class Settings(BaseSettings):
    # ── Deployment environment label ──────────────────────────────────────
    # Stamped into every audit record and surfaced in /healthz so an operator
    # can confirm a "dev" container is not pointed at the prod Maximo. Free-
    # form, but the recommended values are dev | staging | prod.
    MAXIMO_ENV: str = "dev"

    # ── Maximo Connection ──────────────────────────────────────────────────
    MAXIMO_URL: str = "https://your-maximo-host.com/maximo/oslc"
    MAXIMO_HOST: str = "https://your-maximo-host.com"

    # Auth mode: apikey | basic | oauth
    AUTH_MODE: Literal["apikey", "basic", "oauth"] = "basic"

    # API Key auth
    MAXIMO_API_KEY: Optional[str] = None

    # Basic auth (username + password)
    MAXIMO_USERNAME: Optional[str] = None
    MAXIMO_PASSWORD: Optional[str] = None

    # OAuth 2.0
    OAUTH_TOKEN_URL: Optional[str] = None
    OAUTH_CLIENT_ID: Optional[str] = None
    OAUTH_CLIENT_SECRET: Optional[str] = None

    # ── Redis Cache ────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_PASSWORD: Optional[str] = None  # Set to enable Redis AUTH
    CACHE_TTL_SECONDS: int = 300
    CACHE_ENABLED: bool = True

    # ── MCP Transport ─────────────────────────────────────────────────────
    TRANSPORT_MODE: Literal["stdio", "http"] = "stdio"
    HTTP_PORT: int = 8080
    HTTP_HOST: str = "0.0.0.0"

    # ── AI Features ───────────────────────────────────────────────────────
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    CHROMA_PERSIST_DIR: str = "./chroma_db"

    # ── Security ──────────────────────────────────────────────────────────
    RBAC_ENABLED: bool = True
    CURRENT_USER_ROLE: str = "technician"  # Set per session / env
    CURRENT_USER_ID: str = "system"
    # MCP HTTP transport bearer token (Authorization: Bearer <token>).
    # Leave unset to disable auth (stdio mode or trusted network only).
    MCP_ACCESS_TOKEN: Optional[str] = None

    # ── Inbound auth mode (HTTP/SSE only) ─────────────────────────────────
    # static — single shared MCP_ACCESS_TOKEN; identity from X-MCP-* headers
    #          (gateway-injected) or env defaults
    # jwt    — every request must carry a valid OIDC JWT; identity from claims
    # both   — accept either (JWT takes precedence, falls back to static)
    MCP_AUTH_MODE: Literal["static", "jwt", "both"] = "static"

    OIDC_ISSUER: Optional[str] = None
    OIDC_AUDIENCE: Optional[str] = None
    # If unset, derived as f"{OIDC_ISSUER}/.well-known/jwks.json"
    OIDC_JWKS_URL: Optional[str] = None
    OIDC_ALGORITHMS: List[str] = ["RS256"]
    OIDC_JWKS_CACHE_TTL_SECONDS: int = 3600

    # Claim names to map onto the per-request Identity (see core/identity.py)
    OIDC_USER_CLAIM: str = "sub"
    OIDC_ROLE_CLAIM: str = "role"
    OIDC_TENANT_CLAIM: str = "tenant"

    # ── CORS (HTTP/SSE only) ──────────────────────────────────────────────
    # Browser MCP clients require CORS. Default is an empty allowlist =
    # no cross-origin access. Configure explicit origins per environment.
    # Wildcard "*" is rejected when MCP_CORS_ALLOW_CREDENTIALS=true.
    MCP_ALLOWED_ORIGINS: List[str] = []
    MCP_CORS_ALLOW_CREDENTIALS: bool = False
    MCP_CORS_ALLOW_METHODS: List[str] = ["GET", "POST", "OPTIONS"]
    MCP_CORS_ALLOW_HEADERS: List[str] = [
        "Authorization",
        "Content-Type",
        "X-Request-Id",
        "X-MCP-User-Id",
        "X-MCP-Tenant-Id",
        "X-MCP-Role",
    ]

    # ── Tool discovery (HTTP/SSE only) ────────────────────────────────────
    # When False, /v1/tools and /v1/providers/* return 404. The full tool
    # surface — names, descriptions, parameter schemas — is a useful map
    # for legitimate clients but also a perfect blueprint for a leaked
    # bearer token. Disable in production unless the deployment is paired
    # with per-caller OIDC tokens (G6) so leaks are scoped and revocable.
    MCP_DISCOVERY_ENABLED: bool = True

    # ── In-process TLS (HTTP/SSE only) ────────────────────────────────────
    # Optional. If both are set, uvicorn binds with TLS instead of plaintext.
    # Recommended path is still TLS at the edge (ALB / Front Door / API GW);
    # in-process TLS is for single-tenant operators without an edge.
    MCP_SSL_CERTFILE: Optional[str] = None
    MCP_SSL_KEYFILE: Optional[str] = None

    # ── Output PII Masking ────────────────────────────────────────────────
    # Applied in core/maximo_client._request before responses leave the trust
    # boundary toward an LLM caller. See core/pii.py for default field set.
    PII_MASK_ENABLED: bool = True
    PII_MASK_FIELDS: List[str] = []  # extra leaf field names to mask
    PII_MASK_VALUE: str = "***MASKED***"

    # ── Audit & Logging ───────────────────────────────────────────────────
    AUDIT_LOG_FILE: str = "./logs/audit.jsonl"
    # AUDIT_SINK selects the persistence destination — see core/audit_sinks.py.
    #   file   — JSONL on local disk with size-based rotation (default).
    #   stdout — JSONL to stdout for container-runtime / sidecar log shipping.
    #   both   — write to file AND stdout (e.g. during a migration window).
    AUDIT_SINK: Literal["file", "stdout", "both"] = "file"
    AUDIT_FILE_MAX_BYTES: int = 50_000_000   # rotate at ~50 MB
    AUDIT_FILE_BACKUP_COUNT: int = 5         # keep audit.jsonl.1 .. .5
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    # ── Rate Limiting ─────────────────────────────────────────────────────
    RATE_LIMIT_PER_MINUTE: int = 60

    # ── VPN / High-Latency Safe Mode ──────────────────────────────────────
    # Enable for VPN environments to reduce default payload sizes.
    # Tools that do not set page_size explicitly will use DEFAULT_PAGE_SIZE.
    # Users may still override per-call by passing page_size explicitly.
    VPN_SAFE_MODE: bool = True
    DEFAULT_PAGE_SIZE: int = 20  # Active when VPN_SAFE_MODE=true

    # ── HTTP Client ───────────────────────────────────────────────────────
    # Large OSLC queries (e.g. work orders) can exceed 30s on busy servers.
    # Backward compatible: if set, used as read timeout.
    HTTP_TIMEOUT_SECONDS: int = 30
    HTTP_CONNECT_TIMEOUT_SECONDS: float = 3.0
    HTTP_READ_TIMEOUT_SECONDS: float = 30.0
    HTTP_WRITE_TIMEOUT_SECONDS: float = 30.0
    HTTP_POOL_TIMEOUT_SECONDS: float = 30.0

    HTTP_MAX_RETRIES: int = 3
    HTTP_RETRY_BACKOFF_BASE_SECONDS: float = 0.8
    HTTP_RETRY_BACKOFF_MAX_SECONDS: float = 15.0

    # Connection pooling/keepalive: stale keepalives can trigger ReadError/connection reset
    HTTP_MAX_CONNECTIONS: int = 50
    HTTP_MAX_KEEPALIVE_CONNECTIONS: int = 0   # 0 = close after each request, no stale-reuse resets
    HTTP_KEEPALIVE_EXPIRY_SECONDS: float = 10.0

    # Observability
    DEBUG_HTTP: bool = False

    @model_validator(mode="after")
    def validate_auth_config(self) -> "Settings":
        if self.AUTH_MODE == "apikey" and not self.MAXIMO_API_KEY:
            raise ValueError("MAXIMO_API_KEY is required when AUTH_MODE=apikey")
        if self.AUTH_MODE == "basic":
            if not self.MAXIMO_USERNAME or not self.MAXIMO_PASSWORD:
                raise ValueError(
                    "MAXIMO_USERNAME and MAXIMO_PASSWORD are required when AUTH_MODE=basic"
                )
        if self.AUTH_MODE == "oauth":
            if not all([self.OAUTH_TOKEN_URL, self.OAUTH_CLIENT_ID, self.OAUTH_CLIENT_SECRET]):
                raise ValueError(
                    "OAUTH_TOKEN_URL, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET are required when AUTH_MODE=oauth"
                )
        if self.MCP_AUTH_MODE in ("jwt", "both"):
            if not (self.OIDC_ISSUER and self.OIDC_AUDIENCE):
                raise ValueError(
                    "OIDC_ISSUER and OIDC_AUDIENCE are required when MCP_AUTH_MODE is 'jwt' or 'both'"
                )
        if self.MCP_CORS_ALLOW_CREDENTIALS and "*" in self.MCP_ALLOWED_ORIGINS:
            raise ValueError(
                "MCP_ALLOWED_ORIGINS cannot contain '*' when "
                "MCP_CORS_ALLOW_CREDENTIALS=true (browsers reject this combination)."
            )
        if bool(self.MCP_SSL_CERTFILE) ^ bool(self.MCP_SSL_KEYFILE):
            raise ValueError(
                "MCP_SSL_CERTFILE and MCP_SSL_KEYFILE must be set together."
            )
        return self

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}")
        return v.upper()

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


# Singleton instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
