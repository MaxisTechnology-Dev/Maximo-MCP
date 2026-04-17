# Security Policy

## Supported Deployment Model

This project is intended to be deployed in one of two ways:

- Local `stdio` MCP for a single trusted developer session.
- Hosted HTTP/SSE plus FastAPI behind authentication, TLS, and network controls.

Do not expose the raw hosted service directly to the public internet without an API gateway or reverse proxy.

## Secrets

- Never commit real `.env`, `.mcp.json`, or `.cursor/mcp.json` values.
- Use the `*.example` files as templates only.
- Store production secrets in a secret manager such as AWS Secrets Manager or Azure Key Vault.

## Hosted Mode Requirements

Hosted mode requires `MCP_ACCESS_TOKEN` (or a configured OIDC issuer when
`MCP_AUTH_MODE=jwt`).

### Mandatory controls

- **TLS in transit.** The application does NOT terminate TLS by itself. You
  MUST front it with TLS termination — either at the edge (ALB, Front Door,
  API Gateway, NGINX, Caddy, etc.) or by setting `MCP_SSL_CERTFILE` and
  `MCP_SSL_KEYFILE` so uvicorn binds with TLS in-process. Plaintext HTTP
  exposure on a public network is unsupported.
- **Inbound auth.** `MCP_AUTH_MODE` must be `static`, `jwt`, or `both`, and
  the corresponding credentials must be configured. `server.py` fails closed
  at startup if requirements are not met.
- **CORS allowlist.** `MCP_ALLOWED_ORIGINS` defaults to empty (no
  cross-origin access). Set it explicitly per environment for browser
  clients. `*` is rejected when `MCP_CORS_ALLOW_CREDENTIALS=true`.

### Recommended additional controls

- network-level access restrictions (private subnet, security groups)
- rate limiting (`RATE_LIMIT_PER_MINUTE`) and a separate edge rate limiter
- audit log review (forwarded to durable storage — see PRODUCT_GAPS G8)
- private connectivity to Maximo where possible

## RBAC Note

The current role model is environment-driven and suitable for local or trusted deployments. It is not a complete multi-tenant identity solution by itself.

## Reporting Security Issues

If you discover a security issue, treat it as sensitive and avoid publishing exploit details in public issues until a fix is available.
