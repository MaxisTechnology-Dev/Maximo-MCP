# Maximo Enterprise MCP

<!-- mcp-name: io.github.MaxisTechnology-Dev/maximo-mcp -->

A production-focused integration that brings IBM Maximo Asset Management into AI workflows through the Model Context Protocol.

Built by [Maxis Technology](https://maxistechnology.com) as part of **Alchemize** — a state-of-the-art enterprise data management platform capable of doing in hours what others do in days. Want to know more? Head over to [alchemize.io](https://alchemize.io/).

This project now exposes:

- A stable MCP server for Claude Desktop, Cursor, and other MCP clients.
- A hosted HTTP/SSE mode for remote MCP access.
- A FastAPI tool layer for OpenAI, Gemini, Grok, and custom orchestrators.

The current stable surface is `31` public tools across assets, work orders, inventory, purchasing, labor, locations, reporting, schema discovery, and administration.

## Responsible Use

This server speaks to a live IBM Maximo instance and exposes its data — and,
when explicitly enabled, its mutating operations — to a language model. Before
deploying:

- Run hosted HTTP/SSE behind an authenticated gateway. Per-request identity
  comes from `MCP_AUTH_MODE=jwt` (OIDC) or static + gateway-injected
  `X-MCP-*` headers. Never expose the FastAPI tool API to untrusted callers
  without an identity solution in front.
- TLS at the edge (or in-process via `MCP_SSL_*`) is mandatory for any
  non-stdio deployment. The app does not terminate TLS itself.
- Keep all write tools `# DISABLED` in [server.py](server.py) until you have
  reviewed the RBAC policy and audit posture for your environment.
- Stamp `MAXIMO_ENV=dev|staging|prod` per deployment so audit records and
  `/healthz` make it obvious which Maximo a container is talking to.
- Read [SECURITY.md](SECURITY.md) and
  [PRODUCT_GAPS_BEFORE_DEPLOY.md](PRODUCT_GAPS_BEFORE_DEPLOY.md) before
  pushing to a public registry; the latter documents the exact controls
  required for `https://github.com/mcp`-style listings.

## Architecture

```text
Local MCP clients        -> stdio MCP server -> Maximo OSLC
Remote MCP clients       -> HTTP gateway -> hosted MCP SSE -> Maximo OSLC
OpenAI / Gemini / Grok   -> FastAPI tool API -> shared executor -> Maximo OSLC
```

## Installation

### Local MCP for Claude Desktop / Claude Code / Cursor

You have two ways to install: `uvx` (recommended — no manual install) or `pip install`.

#### Option A — `uvx` (recommended)

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) once, then point your MCP client at `uvx maximo-enterprise-mcp`. `uvx` downloads, caches, and runs the package on demand.

#### Option B — `pip install`

```bash
pip install maximo-enterprise-mcp
```

Then in the configs below, replace:

```json
"command": "uvx",
"args": ["maximo-enterprise-mcp"]
```

with:

```json
"command": "maximo-enterprise-mcp",
"args": []
```

#### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "maximo": {
      "command": "uvx",
      "args": ["maximo-enterprise-mcp"],
      "env": {
        "MAXIMO_URL": "https://your-maximo-host.com/maximo/oslc",
        "MAXIMO_HOST": "https://your-maximo-host.com",
        "AUTH_MODE": "basic",
        "MAXIMO_USERNAME": "your-username",
        "MAXIMO_PASSWORD": "your-password",
        "CURRENT_USER_ROLE": "readonly"
      }
    }
  }
}
```

Restart Claude Desktop. The hammer icon appears once tools load.

#### Claude Code

Add to project-level `.mcp.json` or global `~/.claude.json`:

```json
{
  "mcpServers": {
    "maximo": {
      "command": "uvx",
      "args": ["maximo-enterprise-mcp"],
      "env": {
        "MAXIMO_URL": "https://your-maximo-host.com/maximo/oslc",
        "MAXIMO_HOST": "https://your-maximo-host.com",
        "AUTH_MODE": "basic",
        "MAXIMO_USERNAME": "your-username",
        "MAXIMO_PASSWORD": "your-password",
        "CURRENT_USER_ROLE": "readonly"
      }
    }
  }
}
```

Run `/mcp` in Claude Code to verify the connection.

#### Cursor

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per project):

```json
{
  "mcpServers": {
    "maximo": {
      "command": "uvx",
      "args": ["maximo-enterprise-mcp"],
      "env": {
        "MAXIMO_URL": "https://your-maximo-host.com/maximo/oslc",
        "MAXIMO_HOST": "https://your-maximo-host.com",
        "AUTH_MODE": "basic",
        "MAXIMO_USERNAME": "your-username",
        "MAXIMO_PASSWORD": "your-password",
        "CURRENT_USER_ROLE": "readonly"
      }
    }
  }
}
```

In Cursor: Settings → MCP → Refresh to load.

### Hosted MCP over HTTP/SSE

Hosted mode is intended for trusted network or gateway-protected deployments. It now fails closed unless `MCP_ACCESS_TOKEN` is set.

```bash
python server.py --http --host 0.0.0.0 --port 8080
```

Remote MCP clients should connect to:

- MCP SSE endpoint: `http://host:8080/sse`
- Health endpoint: `http://host:8080/healthz`

Example remote MCP config:

```json
{
  "mcpServers": {
    "maximo": {
      "type": "sse",
      "url": "http://localhost:8080/sse",
      "headers": {
        "Authorization": "Bearer <your MCP_ACCESS_TOKEN>"
      }
    }
  }
}
```

### FastAPI Tool API

Hosted HTTP mode also exposes a tool API for non-MCP platforms:

- `GET /healthz`
- `GET /v1/tools`
- `POST /v1/tools/{tool_name}`
- `GET /v1/providers/openai-tools`
- `GET /v1/providers/gemini-tools`
- `GET /v1/providers/grok-tools`

Example invocation:

```bash
curl -X POST http://localhost:8080/v1/tools/list_assets \
  -H "Authorization: Bearer $MCP_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"site_id\":\"BEDFORD\",\"page_size\":10}"
```

## AI Platform Usage

### OpenAI

Use `GET /v1/providers/openai-tools` to retrieve OpenAI-compatible tool definitions, then execute the selected tool via `POST /v1/tools/{tool_name}`.

### Gemini

Use `GET /v1/providers/gemini-tools` to retrieve Gemini function declarations from the same shared tool registry.

### Grok

Use `GET /v1/providers/grok-tools`. The payload is OpenAI-compatible so the same orchestration pattern works.

## Optional Dependencies

Core installation uses `requirements.txt`.

Optional extras are defined in [`pyproject.toml`](pyproject.toml):

- `pip install ".[ai]"` for OpenAI, ChromaDB, and sentence-transformers.
- `pip install ".[exports]"` for Excel and PDF export dependencies.
- `pip install ".[dev]"` for local test and lint tooling.

## Environment Variables

See [`.env.example`](.env.example) for the complete set. The most important variables are:

| Variable | Required | Description |
|---|---|---|
| `MAXIMO_URL` | Yes | Full Maximo OSLC base URL |
| `MAXIMO_HOST` | Yes | Maximo host root URL |
| `AUTH_MODE` | Yes | `basic`, `apikey`, or `oauth` |
| `MAXIMO_USERNAME` / `MAXIMO_PASSWORD` | Basic auth | Maximo credentials |
| `MCP_ACCESS_TOKEN` | Hosted HTTP | Required bearer token for hosted MCP/API mode |
| `CURRENT_USER_ROLE` | Local only | Session role for local or trusted deployments |
| `REDIS_URL` | No | Redis cache connection string |
| `VPN_SAFE_MODE` | No | Enables safer default payload sizes |
| `DEFAULT_PAGE_SIZE` | No | Default page size when safe mode is enabled |

## Docker

### Build

```bash
docker build -t maximo-enterprise-mcp .
```

### Run

```bash
docker run -d ^
  -p 8080:8080 ^
  -e TRANSPORT_MODE=http ^
  -e MCP_ACCESS_TOKEN=change-me ^
  -e MAXIMO_URL=https://your-maximo-host.example.com/maximo/oslc ^
  -e MAXIMO_HOST=https://your-maximo-host.example.com ^
  -e AUTH_MODE=basic ^
  -e MAXIMO_USERNAME=your-maximo-username ^
  -e MAXIMO_PASSWORD=your-maximo-password ^
  maximo-enterprise-mcp
```

Use [`docker-compose.yml`](docker-compose.yml) if you want Redis included.

## Deployment Guidance

The server does NOT terminate TLS itself. Every non-stdio deployment
MUST terminate TLS either at the edge or in-process via
`MCP_SSL_CERTFILE` / `MCP_SSL_KEYFILE`. See [SECURITY.md](SECURITY.md).

### AWS

- Recommended first target: ECS Fargate or App Runner.
- Store secrets in AWS Secrets Manager.
- **Required:** put ALB or API Gateway in front for TLS and access control.
- Keep Maximo connectivity private when possible.

### Azure

- Recommended first target: Azure Container Apps or App Service.
- Store secrets in Key Vault.
- **Required:** put Application Gateway or Front Door in front for TLS and access control.

### Environment Separation

Run one container image, three deployments, three secret stores. Stamp
`MAXIMO_ENV=dev|staging|prod` per deployment — the value lands in every
audit record and on `/healthz`, so an operator can confirm at a glance
which Maximo a given container is pointed at.

Recommended pattern:

| Deployment | `MAXIMO_ENV` | Maximo target | Token scope |
|---|---|---|---|
| dev      | `dev`     | dev Maximo     | dev `MCP_ACCESS_TOKEN` / OIDC tenant |
| staging  | `staging` | staging Maximo | staging token / OIDC tenant |
| prod     | `prod`    | prod Maximo    | prod token / OIDC tenant |

Never reuse a token across environments — a leaked dev token must not
unlock prod, and a misrouted dev container must not write to prod data.

### Public Internet

Exposing the raw hosted MCP to the public internet requires ALL of the
following — no exceptions:

- bearer or OIDC inbound authentication (`MCP_AUTH_MODE`)
- TLS at the edge or in-process (the app serves plaintext by default)
- explicit CORS allowlist (`MCP_ALLOWED_ORIGINS`) — do NOT use `*`
- rate limiting (`RATE_LIMIT_PER_MINUTE`) and a separate edge limiter
- audit logging with durable forwarding
- private Maximo connectivity or strict network controls

## Development

### Run unit tests

```bash
pytest -m "not integration"
```

### Run integration tests

Integration tests hit a real Maximo instance configured in `.env`.

```bash
pytest tests/integration_test_tools.py -m integration -v
```

### List registered MCP tools

```bash
python server.py --test
```

## Security Notes

- Do not commit real `.env`, `.mcp.json`, or `.cursor/mcp.json` values.
- Use example configs from the `*.example` files and keep secrets in local-only files or a secret manager.
- Hosted mode requires `MCP_ACCESS_TOKEN`.
- The env-based role model is suitable for local or trusted deployments, not multi-tenant public hosting.

---

*IBM and Maximo are trademarks of International Business Machines Corp., used here for descriptive purposes only. This project is not affiliated with IBM.*