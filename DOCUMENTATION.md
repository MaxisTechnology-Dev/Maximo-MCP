# Maximo Enterprise MCP — Complete Documentation

> **Version:** 2.0 | **Transport:** stdio / HTTP SSE | **Tools:** 59 | **Built with:** FastMCP + Python 3.11

---

## Table of Contents

1. [Architecture Diagram](#1-architecture-diagram)
2. [Architecture Document](#2-architecture-document)
3. [User Documentation](#3-user-documentation)
4. [Developer Documentation](#4-developer-documentation)
5. [API / Tool Reference](#5-api--tool-reference)
6. [Operations & Deployment Guide](#6-operations--deployment-guide)

---

## 1. Architecture Diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                        MAXIMO ENTERPRISE MCP — SYSTEM ARCHITECTURE          ║
╚══════════════════════════════════════════════════════════════════════════════╝

  ┌─────────────────────────────────────────────────────────────────────────┐
  │                         AI CLIENT LAYER                                  │
  │                                                                           │
  │   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐                │
  │   │ Claude.ai /  │   │  Cursor IDE  │   │  VS Code /   │                │
  │   │ Claude Code  │   │  (MCP client)│   │  Other MCP   │                │
  │   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘                │
  └──────────┼───────────────────┼──────────────────┼──────────────────────-─┘
             │   stdio / SSE     │   stdio / SSE    │   stdio / SSE
             ▼                   ▼                  ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                      MCP SERVER (FastMCP)                                │
  │                        server.py                                          │
  │                                                                           │
  │   • Tool registration (59 tools)      • RBAC enforcement                 │
  │   • Request routing                   • Audit logging                    │
  │   • stdio / HTTP SSE transport        • Rate limiting (60 req/min)       │
  └──────────────────────────────┬──────────────────────────────────────────┘
                                  │
             ┌────────────────────┼────────────────────┐
             ▼                    ▼                    ▼
  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
  │   TOOL LAYER     │  │  CORE SERVICES   │  │    CACHE LAYER           │
  │                  │  │                  │  │                          │
  │ tools/           │  │ core/            │  │  Redis (primary)         │
  │  assets.py       │  │  maximo_client   │  │  ┌──────────────────┐   │
  │  workorders.py   │  │  settings        │  │  │ TTL: 5min default│   │
  │  pm_scheduling   │  │  cache           │  │  │ Schema: 24 hrs   │   │
  │  inventory.py    │  │  rbac            │  │  │ 256MB max LRU    │   │
  │  purchasing.py   │  │  audit           │  │  └──────────────────┘   │
  │  labor.py        │  │  auth            │  │                          │
  │  locations.py    │  │  rag_engine      │  │  In-Memory Fallback      │
  │  ai_intelligence │  │                  │  │  (when Redis offline)    │
  │  reporting.py    │  └────────┬─────────┘  └────────┬─────────────────┘
  │  admin.py        │           │                     │
  │  schema_dev.py   │           │                     │
  │  integrations.py │           │◄────────────────────┘
  └──────────────────┘           │
                                  │ HTTP (OSLC REST / JSON)
                                  │ Basic Auth / API Key / OAuth
                                  │ Retry: 3x + exponential backoff
                                  ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                    NETWORK BOUNDARY (VPN / Firewall)                     │
  └─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                   IBM MAXIMO (Application Server)                        │
  │                   http://<host>:9080/maximo                              │
  │                                                                           │
  │   OSLC Object Structures:                                                 │
  │   /os/mxasset    /os/mxwo       /os/mxapipm     /os/mxinventory         │
  │   /os/mxpo       /os/mxperson   /os/mxoperloc   /os/mxlabor             │
  │   /os/mxreceipt  /os/mxinvtrans /os/mxmatrectrans                        │
  │                                                                           │
  │   Authentication: /oslc/j_security_check | /oslc/login                   │
  │   Metadata:       /api/metadata/<OS> (v7.6.1.2+)                        │
  │   Whoami:         /oslc/whoami                                           │
  └─────────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
User Prompt → MCP Client → [stdio/SSE] → FastMCP Router
    → RBAC Check → Tool Function → Cache Lookup
        → [cache hit] → Return cached response
        → [cache miss] → MaximoClient → OSLC API → Maximo DB
            → Response → Cache store → Envelope → MCP Client → User
```

---

## 2. Architecture Document

### 2.1 Overview

**Maximo Enterprise MCP** is an open-source Model Context Protocol server that bridges AI assistants (Claude, Cursor, VS Code Copilot) with IBM Maximo Asset Management. It exposes 59 purpose-built tools covering the full maintenance lifecycle — from asset lifecycle management and work order orchestration to AI-powered root cause analysis and IoT alert ingestion.

**Core capabilities:**
- Read, create, update, and retire assets across any Maximo site
- Full work order lifecycle: create → approve → assign → close → cancel
- Preventive maintenance scheduling and PM work order generation
- Inventory management, purchase orders, and vendor analytics
- AI analytics: anomaly detection, root cause analysis, health scoring
- Natural language → OSLC query translation
- Schema discovery and API code generation
- Event subscriptions and IoT/SCADA integration
- Role-based access control with 5-tier hierarchy
- Full audit trail of every tool invocation

---

### 2.2 System Components

#### MCP Server (`server.py`)
The entry point built on **FastMCP 2.x**. Responsibilities:
- Registers all 59 tools with the MCP protocol
- Manages transport (stdio for Claude/Cursor, HTTP SSE for web)
- Parses CLI arguments (`--http`, `--test`, `--port`)
- Bootstraps settings, logging, and the tool registry

Supported transports:
| Mode | Protocol | Use Case |
|---|---|---|
| `stdio` | JSON-RPC over stdin/stdout | Claude Desktop, Cursor, VS Code |
| `http` | HTTP SSE (Server-Sent Events) | Docker, web clients, multi-user |

#### Tool Layer (`tools/`)
14 domain modules, each a plain Python module of `async def` functions. Every tool registered in `core/tool_catalog.py` has a strict `extra="forbid"` Pydantic input model in `core/tool_models.py`.

| Module | Domain | Active tools |
|---|---|---:|
| `assets.py` | Asset lifecycle, reliability, warranty | 9 |
| `workorders.py` | Work orders, job plans, scheduler, costs | 13 |
| `pm_scheduling.py` | Preventive maintenance | (read-only on demo Maximo) |
| `inventory.py` | Stock, item master, storerooms, valuation, critical spares | 8 |
| `purchasing.py` | POs, PRs, vendors, spend analysis | 6 |
| `labor.py` | Labor, crews, crafts, availability finder | 5 |
| `locations.py` | Operational locations + hierarchy | 3 |
| `ai_intelligence.py` | NL-to-OSLC, anomaly detection, RCA, health scoring | 4 |
| `reporting.py` | KPI dashboards, Pareto, bad-actor, Excel + PDF export | 6 |
| `admin.py` | Users, audit log | 3 |
| `schema_dev.py` | Schema discovery, OSLC validation, code generation | 4 |
| `integrations.py` | Events, IoT, webhooks | 1 |
| `compliance.py` | Calibrations, inspections, permits, certs, incidents, dashboard | 6 |
| `verticals.py` | Pharma + Oil & Gas + Mfg + Utilities + Healthcare + Transport | 18 |
| `server.py` | Health check | 1 |
| **Total** | | **87** |

All tool functions return a consistent response envelope:
```json
{
  "success": true,
  "data": { ... },
  "metadata": { "cached": false, "duration_ms": 142 }
}
```

#### Core Services (`core/`)

| Module | Responsibility |
|---|---|
| `maximo_client.py` | Async OSLC HTTP client with auth, retry, and connection pooling |
| `settings.py` | Pydantic `BaseSettings` — loads from `.env`, validates config |
| `cache.py` | Redis-first cache with in-memory dict fallback; supports TTL and pattern invalidation |
| `rbac.py` | `@require_role()` decorator; 5-tier hierarchy from `config/rbac_policies.yaml` |
| `audit.py` | JSONL audit trail; records tool name, user, inputs, outcome, duration |
| `auth.py` | Auth helpers (Basic, API key, OAuth token refresh) |
| `rag_engine.py` | ChromaDB + sentence-transformers for `search_maximo_knowledge` |

#### Cache Layer

**Primary: Redis**
- URL: `redis://localhost:6379`
- Max memory: 256 MB (LRU eviction)
- Persistence: AOF enabled
- Default TTL: 300s (5 minutes)
- Schema cache TTL: 86400s (24 hours)

**Fallback: In-Memory Dict**
Automatically activates when Redis is unavailable. Same interface, same TTL enforcement. Not shared across processes.

#### Maximo Integration

All Maximo communication goes through `MaximoClient` (singleton via `get_connected_client()`):

- **Protocol:** OSLC REST (JSON, `lean=1` mode)
- **Object Structures:** `/os/<MXOS>` endpoints (e.g., `/os/mxwo`, `/os/mxasset`)
- **Query format:** OSLC where/select parameters (`oslc.where`, `oslc.select`)
- **Pagination:** `oslc.pageSize` + `oslc.pagenum`
- **Auth sessions:** Cookie-based; auto re-login on session expiry
- **Connection pooling:** 50 max connections, 10 keepalive, 20s keepalive expiry

#### Authentication & Security

Three auth modes supported via `AUTH_MODE` env var:

| Mode | Config Required | Use Case |
|---|---|---|
| `basic` | `MAXIMO_USERNAME` + `MAXIMO_PASSWORD` | Dev, lab environments |
| `apikey` | `MAXIMO_API_KEY` | Maximo 7.6.1+ production |
| `oauth` | `OAUTH_TOKEN_URL` + client ID/secret | Enterprise SSO |

**RBAC (Role-Based Access Control):**

```
admin
  └── manager
        └── supervisor
              └── technician
                    └── readonly
```

Set the active role via `CURRENT_USER_ROLE` env var. The `@require_role("supervisor")` decorator enforces minimum role at tool call time. Policies are defined in `config/rbac_policies.yaml`.

---

### 2.3 Data Flow — End-to-End Request Lifecycle

```
1. User types: "Create a work order for pump P-101 at site BEDFORD"

2. Claude resolves → calls MCP tool: create_workorder(
       description="Pump P-101 inspection",
       asset_num="P-101",
       site_id="BEDFORD",
       work_type="CM",
       priority=3
   )

3. FastMCP routes → server.py create_workorder() → tools/workorders.create_workorder()

4. RBAC: @require_role("technician") checked against CURRENT_USER_ROLE

5. Audit: pre-call record written to logs/audit.jsonl

6. Cache check: key = "maximo:wo:create" — not applicable for writes

7. MaximoClient.post("/os/mxwo", body={...})
   → HTTP POST to https://your-maximo-host.example.com/maximo/oslc/os/mxwo
   → Maximo validates asset, site, work type
   → Returns new WO record with assigned wonum

8. Response envelope built:
   {"success": true, "data": {"wonum": "WO10042", "siteid": "BEDFORD", ...}}

9. Audit: post-call record updated with success/duration

10. FastMCP serializes → returns to Claude over stdio

11. Claude: "I created work order WO10042 for pump P-101 at BEDFORD."
```

---

### 2.4 Design Decisions

**Why MCP?**
MCP is the emerging standard for AI tool integration. It provides structured tool discovery, typed inputs/outputs, and works natively with Claude, Cursor, and VS Code Copilot without custom prompt engineering. It eliminates the need to embed Maximo API knowledge into prompts.

**Why OSLC?**
IBM Maximo's native REST API is OSLC-compliant. OSLC object structures are more stable than the JSON API and provide the richest query capability (`oslc.where` filters, relationship traversal). Using OSLC directly avoids dependencies on third-party Maximo client libraries.

**Why caching?**
Maximo OSLC queries are expensive — large object structures like `mxwo` with 3M+ records can take 30–90s without filters. Caching list results for 5 minutes and schema results for 24 hours reduces latency by 90%+ on repeat queries without stale data risk for operational decisions.

**Retry/backoff strategy:**
Maximo's WebSphere application server drops TCP connections under load. The client implements 3 retries with exponential backoff (base 0.8s, max 15s, ±10% jitter) and automatic re-authentication on session expiry. Transport errors reset the connection pool before retrying.

---

### 2.5 Scalability & Deployment

| Scenario | Recommended Config |
|---|---|
| Local / single developer | stdio transport, in-memory cache |
| Team (2–5 users) | HTTP SSE transport, Redis cache, single Docker container |
| Production / multi-tenant | Docker Compose, Redis cluster, load balancer, `CURRENT_USER_ROLE` per session |

**Redis is required for multi-user deployments** to share cache across processes. Without Redis, each worker has its own in-memory cache and Maximo will receive redundant queries.

---

### 2.6 Limitations

| Limitation | Detail |
|---|---|
| Maximo server stability | Large OSLC queries (mxwo, mxasset) cause TCP drops under JVM memory pressure. Requires sufficient `-Xmx` on Maximo side. |
| PM permissions | All PM tools require READ/WRITE on the PM business object in Maximo security. |
| `search_maximo_knowledge` | Requires `chromadb` + `sentence-transformers` — adds ~1.2 GB to the container. Optional. |
| `build_custom_object_structure` | Requires `MXOS` base OS to exist in this Maximo instance. |
| VPN latency | Tools targeting large object structures add 60–200ms per hop for VPN deployments. |
| Object structure variants | PM OS can be `mxapipm` or `mxpm` depending on Maximo version. Auto-detected at first call. |
| No real-time streaming | Tool responses are request/response only. Long-running Maximo jobs are fire-and-confirm. |

---

## 3. User Documentation

### 3.1 Getting Started

#### Prerequisites
- Python 3.11+
- Access to an IBM Maximo instance (7.6.0+) on the network
- Claude Desktop, Cursor, or VS Code with MCP support

#### 1. Clone and install

```bash
git clone https://github.com/your-org/maximo-enterprise-mcp.git
cd maximo-enterprise-mcp
pip install -r requirements.txt
```

#### 2. Configure `.env`

Copy and edit the environment file:

```bash
cp .env .env.local
```

Minimum required settings:

```env
# Maximo connection
MAXIMO_URL=http://your-maximo-host:9080/maximo/oslc
MAXIMO_HOST=http://your-maximo-host:9080
AUTH_MODE=basic
MAXIMO_USERNAME=maxadmin
MAXIMO_PASSWORD=your_password

# Your role (controls which tools are available)
CURRENT_USER_ROLE=admin
CURRENT_USER_ID=maxadmin
```

#### 3. Test the connection

```bash
python server.py --test
```

Expected output:
```
============================================================
  Maximo Enterprise MCP — Registered Tools
============================================================
   1. approve_workorder
   2. assign_technician
   ...
  59. validate_oslc_query

  Total: 59 tools registered
============================================================
```

#### 4. Connect with Claude Desktop

Add to `~/.claude/claude_desktop_config.json` (Mac/Linux) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "maximo": {
      "command": "python",
      "args": ["C:/path/to/maximo-enterprise-mcp/server.py"],
      "env": {
        "MAXIMO_URL": "https://your-maximo-host.example.com/maximo/oslc",
        "MAXIMO_HOST": "https://your-maximo-host.example.com",
        "AUTH_MODE": "basic",
        "MAXIMO_USERNAME": "your-maximo-username",
        "MAXIMO_PASSWORD": "your-maximo-password",
        "CURRENT_USER_ROLE": "readonly"
      }
    }
  }
}
```

Restart Claude Desktop. Look for the hammer icon (🔨) indicating MCP tools are available.

#### 5. Connect with Cursor

Add to `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "maximo": {
      "command": "python",
      "args": ["server.py"]
    }
  }
}
```

Ensure `.env` is present in the project root.

---

### 3.2 Using Tools — Example Prompts

These are the kinds of asks practitioners run against the MCP. Every prompt naturally chains 2–4 tools the LLM picks up automatically.

#### Asset management & reliability

```
"List all assets at site BEDFORD"
"Show me asset 1001 details at BEDFORD plus its work-order history and meter readings"
"Calculate MTBF, MTTR, and availability for PUMP-007 at BEDFORD over 12 months"
"Run an asset criticality matrix for BEDFORD and summarize health for the top 5"
"Pull the failure-class hierarchy under PUMPS"
"Show the warranty status of all BEDFORD assets — bucket by EXPIRED / EXPIRING_SOON / ACTIVE"
```

#### Work orders, planning & scheduling

```
"Show all open work orders for site BEDFORD this quarter"
"Show me my assigned work orders for today and break down the highest-priority one"
"Pull the schedule calendar for next week at BEDFORD and find me an available welder"
"Compare estimated vs actual cost for WO 1119"
"Pull job plan '12 MPH RED' with full task / labor / material breakdown"
"Estimate the cost of executing job plan PUMP-SEAL-REPLACE before issuing the WO"
```

#### Inventory, purchasing & finance

```
"Show low-stock items at BEDFORD and the open POs that should replenish them"
"Run an inventory valuation for BEDFORD — give me the top 20 items by line value"
"Critical-spares check: for priority-1/2 assets at BEDFORD, are required spares in stock?"
"List purchase requisitions waiting for approval at BEDFORD"
"Run spend analysis on BEDFORD for the last 24 months grouped by vendor"
"Pull vendor performance for whoever's our top spend"
```

#### AI intelligence & root cause

```
"Detect statistical anomalies in PUMP-007's failure history over the last 90 days"
"Suggest a root cause for: pump P-101 showing unexpected vibration and overheating"
"Generate a health score summary for asset COMP-001 at BEDFORD"
"Translate to OSLC: show me all priority 1 work orders created this week in BEDFORD"
"Run a failure Pareto for BEDFORD and tell me what 20% of failure modes cause 80% of work"
"Show the top 5 bad-actor assets at BEDFORD ranked by corrective WO count and labor cost"
```

#### Compliance & EHS

```
"Run the compliance dashboard for BEDFORD"
"List calibrations due in the next 60 days"
"List labor certifications expiring in the next 90 days"
"Show open safety incidents at BEDFORD this quarter"
"List inspections due and overdue for site BEDFORD"
```

#### Industry verticals

**Pharma**

```
"Pull the calibration audit trail for asset 1001 at BEDFORD over 24 months — FDA prep"
"Show me cleanroom assets at BEDFORD"
"Run GxP compliance status — give me the risk score and rating"
```

**Oil & gas**

```
"Show me turnaround status at BEDFORD — top 5 parents with most child WOs"
"List pressure vessels with inspections due in the next 90 days"
"Pull the lifting register for the last 12 months at BEDFORD"
```

**Manufacturing**

```
"Run OEE for BEDFORD over the last 30 days"
"Show production line status for BEDFORD — open WOs and downtime per location"
"List changeover work orders this quarter and average changeover time"
```

**Utilities**

```
"Run outage impact analysis for asset 1001 at BEDFORD — what's downstream?"
"List every asset in grid zone BR450 at BEDFORD"
"Compute SAIDI / SAIFI proxies for BEDFORD over the last 12 months"
```

**Healthcare**

```
"List medical devices with PM/calibration due in the next 30 days"
"Show device lifecycle status for BEDFORD — bucket by NEW / STABLE / AGING / EOL"
"Run Joint Commission environment-of-care rollup for BEDFORD"
```

**Transportation**

```
"Show fleet readiness at BEDFORD — what % of vehicles are operating?"
"Which fleet vehicles have mileage-based PMs coming due?"
"Pull the fuel consumption trend for vehicle TRACK-1 over the last 90 days"
```

#### Reporting & exports

```
"Show the full maintenance KPI dashboard for BEDFORD for the last quarter"
"Export all open work orders at BEDFORD to Excel for the leadership review"
"Generate a PDF asset report for BEDFORD"
"Render the failure Pareto as a Carbon HTML table for our internal portal"
```

#### Schema discovery (for developers)

```
"List all Maximo object structures containing 'asset'"
"Show the field schema for MXWO object structure"
"Validate this OSLC query: mxwo where status='APPR' and siteid='BEDFORD'"
"Generate Python code to list work orders using the Maximo API"
```

---

### 3.3 Best Practices

**Always specify site_id**
Most tools require a valid `site_id`. Omitting it returns all-site data and is significantly slower.

```
✗  "Show me all work orders"
✓  "Show me all open work orders at site BEDFORD"
```

**Use natural language queries for discovery**
The `nl_to_oslc_query` tool translates plain English to OSLC. Use it when you don't know the exact field names:
```
"Convert to OSLC: all work orders with priority 1 created in the last 7 days at BEDFORD"
```

**Paginate large results**
For sites with large datasets (100k+ work orders), specify page limits:
```
"List the first 20 work orders at BEDFORD"         # page_size=20
"Show work orders at BEDFORD, page 2 of 20 per page"
```

**Check health before debugging**
If tools return errors, run a health check first:
```
"Run health_check to verify Maximo connectivity"
```

**Use dry_run for OSLC queries**
When building complex queries, use `dry_run=False` on `nl_to_oslc_query` to validate and execute:
```
"Convert to OSLC and run: show assets with more than 5 failures in 90 days at BEDFORD"
```

---

### 3.4 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "No tools available" in Claude | MCP server not configured | Check `claude_desktop_config.json`; restart Claude |
| `MAXIMO_USERNAME/PASSWORD required` | `.env` not loaded | Verify `.env` is in project root and has correct values |
| `Connection error: ` on all calls | Maximo server unreachable | Check VPN connection; verify `MAXIMO_URL` is correct |
| `API error 400: BMXAA0024E` | Permission denied on Maximo object | Grant READ/WRITE on that business object in Maximo Security |
| `Resource not found (404): /os/mxsecgroup` | Object structure not configured | Create the OS in Maximo → Integration → Object Structures |
| `Java heap space` (HTTP 500) | Maximo JVM out of memory | Increase Maximo `-Xmx` heap; reduce page_size |
| `Request timed out` on `get_schema_details` | MXWO describe takes too long | Use a smaller OS (e.g., `mxlabor`); increase `HTTP_TIMEOUT_SECONDS` |
| `DEPENDENCY_ERROR: chromadb missing` | Vector search deps not installed | `pip install chromadb sentence-transformers` |
| Tools return `success=false, code=RBAC_DENIED` | Insufficient role | Set `CURRENT_USER_ROLE=admin` in `.env` |
| `BMXAA4129E — record already exists` | Duplicate asset/record key | Use a unique `asset_num` (script v2 uses timestamp-based keys) |
| `BMXAA0090E — asset not valid or not OPERATING` | Asset is decommissioned | Filter by `status=OPERATING` in list/search before creating WOs |

---

## 4. Developer Documentation

### 4.1 Project Structure

```
maximo-enterprise-mcp/
├── server.py                   # FastMCP entry point, tool registration
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Container build
├── docker-compose.yml          # MCP server + Redis stack
├── .env                        # Environment config (do not commit)
│
├── core/                       # Infrastructure services
│   ├── maximo_client.py        # Async OSLC HTTP client
│   ├── settings.py             # Pydantic config (loads .env)
│   ├── cache.py                # Redis + in-memory cache
│   ├── rbac.py                 # @require_role() decorator
│   ├── audit.py                # JSONL audit trail writer
│   ├── auth.py                 # Auth helpers (Basic/API key/OAuth)
│   └── rag_engine.py           # ChromaDB semantic search engine
│
├── tools/                      # Domain tool modules
│   ├── assets.py               # Asset CRUD + history + downtime
│   ├── workorders.py           # WO lifecycle + KPIs
│   ├── pm_scheduling.py        # PM list, forecast, generate, update
│   ├── inventory.py            # Stock, transfers, material requests
│   ├── purchasing.py           # POs, receipts, vendor analytics
│   ├── labor.py                # Labor, crews, utilization
│   ├── locations.py            # Locations, hierarchy
│   ├── ai_intelligence.py      # Anomaly, RCA, health, NL→OSLC, RAG
│   ├── reporting.py            # KPI dashboard, Excel/PDF export, Carbon tables
│   ├── admin.py                # Users, security groups, audit log
│   ├── schema_dev.py           # OS discovery, schema, codegen
│   └── integrations.py         # Events, IoT alerts, webhooks
│
├── config/
│   ├── rbac_policies.yaml      # Tool → role mapping
│   └── prompt_templates.py     # NL→OSLC prompt templates
│
├── scripts/
│   └── run_mcp_tool_tests.py   # Full 59-tool integration test runner
│
├── logs/
│   └── audit.jsonl             # Append-only audit log (created at runtime)
│
├── chroma_db/                  # Vector DB for knowledge search (created at runtime)
├── reports/                    # Generated Excel/PDF exports (created at runtime)
└── tests/                      # Unit tests (pytest)
```

---

### 4.2 Adding a New Tool

**Step 1: Define the function in the appropriate domain module**

```python
# tools/assets.py (example: new tool get_asset_warranty)

@require_role("readonly")
async def get_asset_warranty(
    asset_num: str,
    site_id: str,
) -> Dict[str, Any]:
    """
    Retrieve warranty information for an asset.

    Args:
        asset_num: Asset number
        site_id:   Site ID

    Returns:
        Warranty details including expiry date and vendor.
    """
    if not asset_num or not site_id:
        return _error("asset_num and site_id are required", "VALIDATION_ERROR")

    start = time.monotonic()
    cache_key = f"maximo:warranty:{site_id}:{asset_num}"
    cache = get_cache()

    async def fetch():
        client = await get_connected_client()
        params = client.build_oslc_query(
            where=f'assetnum="{asset_num}" and siteid="{site_id}"',
            select="assetnum,warrantyexpdate,vendor,warrantyterm",
        )
        return await client.get("/os/mxasset", params=params)

    try:
        data, cached = await cache.get_or_fetch(cache_key, fetch, ttl=300)
        members = data.get("member", [])
        if not members:
            return _error(f"Asset '{asset_num}' not found", "NOT_FOUND")
        duration_ms = int((time.monotonic() - start) * 1000)
        return _envelope(members[0], cached=cached, duration_ms=duration_ms)
    except (MaximoAPIError, MaximoAuthError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"Unexpected error [{type(exc).__name__}]: {exc!r}", "INTERNAL_ERROR")
```

**Step 2: Register in `server.py`**

```python
@mcp.tool()
async def get_asset_warranty(asset_num: str, site_id: str) -> Dict[str, Any]:
    """Retrieve warranty information for an asset. Includes expiry date and vendor."""
    return await assets.get_asset_warranty(asset_num, site_id)
```

**Step 3: Add RBAC policy in `config/rbac_policies.yaml`**

```yaml
tools:
  get_asset_warranty: [readonly, technician, supervisor, manager, admin]
```

**Step 4: Add a test case in `scripts/run_mcp_tool_tests.py`**

```python
async def t60():
    if not test_asset:
        return {"success": False, "error": "No test asset", "error_code": "TEST_SETUP"}
    return await assets.get_asset_warranty(asset_num=test_asset, site_id=site)

status, _, _ = await _run_tool(60, "get_asset_warranty", t60, timings)
record(60, "get_asset_warranty", status)
```

---

### 4.3 Response Envelope Convention

All tool functions must return a `Dict[str, Any]` with this structure:

**Success:**
```python
def _envelope(data, cached=False, duration_ms=0, record_count=None):
    return {
        "success": True,
        "data": data,
        "metadata": {
            "cached": cached,
            "duration_ms": duration_ms,
            # optional:
            "record_count": record_count,
        }
    }
```

**Failure:**
```python
def _error(message, code="API_ERROR"):
    return {
        "success": False,
        "error": message,
        "error_code": code   # API_ERROR | VALIDATION_ERROR | NOT_FOUND | RBAC_DENIED | EXCEPTION
    }
```

---

### 4.4 Testing

#### Running the full test suite

```bash
# From project root:
python scripts/run_mcp_tool_tests.py
```

The script:
1. Establishes a real Maximo connection (no mocking)
2. Runs all 59 tools sequentially in dependency order
3. Carries test context between tools (asset nums, WO nums, etc.)
4. Prints per-tool status (PASS / FAIL / PARTIAL) and timing
5. Outputs a final category scorecard with pass rate

**Expected runtime:** 20–45 minutes (Maximo OSLC queries are network-bound)

#### Running specific tool categories only

The test script can be edited to run a subset. Each tool is a numbered function `t1()` through `t59()`. Comment out rounds you don't need.

#### Adding a new test case

```python
# In scripts/run_mcp_tool_tests.py — add after the last tool

async def t60():
    if not test_asset:
        return {"success": False, "error": "No test asset", "error_code": "TEST_SETUP"}
    return await assets.get_asset_warranty(asset_num=test_asset, site_id=site)

status, _, _ = await _run_tool(60, "get_asset_warranty", t60, timings)
record(60, "get_asset_warranty", status)
```

Then add the new slot to the categories dict at the bottom:
```python
("Assets", [10, 11, 12, 13, 14, 46, 47, 48, 60]),  # added 60
```

#### Unit tests (`tests/`)

```bash
pytest tests/ -v
```

---

### 4.5 Debugging

**Enable debug HTTP logging:**
```env
DEBUG_HTTP=true
LOG_LEVEL=DEBUG
```

**Common failure patterns:**

| Log Message | Meaning | Action |
|---|---|---|
| `Transport error on GET /os/mxwo; resetting connection` | Maximo dropped the TCP connection | Retry is automatic; if persistent, increase Maximo JVM heap |
| `Basic auth via /login succeeded` | Session expired, re-authenticated | Normal — happens every ~30 min |
| `metadata endpoint failed (400) for MXWO; falling back to /os describe` | `/api/metadata` not supported | Expected on Maximo < 7.6.1.2; fallback is used automatically |
| `RAG dependencies missing (No module named 'chromadb')` | Vector search disabled | `pip install chromadb sentence-transformers` if needed |
| `RBAC policy file not found` | `config/rbac_policies.yaml` missing | All tools accessible but without role enforcement |
| `get_or_fetch: Redis not available, using in-memory fallback` | Redis unreachable | Start Redis or set `CACHE_ENABLED=false` |

**Inspect the audit log:**
```bash
# View last 20 tool calls
tail -20 logs/audit.jsonl | python -m json.tool

# Filter for failures
grep '"success": false' logs/audit.jsonl | head -10
```

**Check registered tools:**
```bash
python server.py --test
```

---

## 5. API / Tool Reference

### 5.1 Connectivity

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `health_check` | Verify Maximo connectivity and server info | — | `maximo_connected`, `maximo_version`, `cache_backend` |

---

### 5.2 Assets (7 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `list_assets` | Paginated asset list with filters | `site_id`, `status`, `asset_type`, `page_size` | `assets[]`, `totalCount` |
| `search_assets` | Keyword search across description + serial | `keyword`, `site_id`, `page_size` | `assets[]`, `totalCount` |
| `get_asset` | Full asset record | `asset_num`, `site_id` | Asset fields: `assetnum`, `description`, `status`, `location`, `serialnum` |
| `get_asset_history` | Work order + failure history | `asset_num`, `site_id`, `lookback_days` | `work_orders[]`, `failure_count` |
| `get_asset_downtime_stats` | MTTR, MTBF, availability % | `asset_num`, `site_id`, `period_months` | `mttr_hours`, `mtbf_hours`, `availability_pct` |
| `create_asset` | Create asset record | `asset_num`, `description`, `site_id` | New asset record. **Role: admin** |
| `update_asset` | Update asset fields | `asset_num`, `site_id`, optional fields | Updated record. **Role: supervisor** |
| `retire_asset` | Decommission asset | `asset_num`, `site_id`, `reason` | Confirmation. **Role: manager** |

---

### 5.3 Work Orders (8 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `list_workorders` | Paginated WO list with filters | `site_id`, `status`, `asset_num`, `priority`, `date_from`, `date_to` | `workorders[]`, `totalCount` |
| `get_workorder` | Full WO details | `wonum`, `site_id` | WO fields: `wonum`, `description`, `status`, `assetnum`, `actlabhrs` |
| `create_workorder` | Create WO | `description`, `asset_num`, `site_id`, `priority`, `work_type` | `wonum`, `siteid`. **Role: technician** |
| `update_workorder` | Update WO fields | `wonum`, `site_id`, optional fields | Updated WO. **Role: technician** |
| `approve_workorder` | Status → APPR | `wonum`, `site_id` | Confirmation. **Role: supervisor** |
| `assign_technician` | Add labor assignment | `wonum`, `site_id`, `labor_code`, `hours_planned` | Assignment record. **Role: supervisor** |
| `close_workorder` | Status → COMP | `wonum`, `site_id`, `actual_hours`, `resolution_notes` | Confirmation. **Role: technician** |
| `cancel_workorder` | Status → CAN | `wonum`, `site_id`, `reason` | Confirmation. **Role: supervisor** |
| `get_workorder_kpis` | KPI metrics | `site_id`, `period_months` | `total_wos`, `completed`, `overdue`, `avg_completion_days`, `backlog` |

---

### 5.4 PM Scheduling (4 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `list_pm_schedules` | Active PM list | `site_id`, `asset_num` | `pm_schedules[]` with `pmnum`, `frequency`, `nextduedate` |
| `get_pm_forecast` | Monthly PM workload forecast | `site_id`, `months_ahead` | `monthly_forecast[]`, `total_scheduled_pms`, `total_estimated_labor_hrs` |
| `generate_pm_workorders` | Trigger PM WO generation | `site_id`, `date_range_days` | Generation confirmation. **Role: supervisor** |
| `update_pm_frequency` | Change PM interval | `pm_num`, `site_id`, `frequency`, `frequency_unit` | Updated PM. **Role: supervisor** |

> **Note:** All PM tools require READ permission on the PM business object in Maximo Security Groups.

---

### 5.5 Inventory (5 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `list_low_stock_items` | Items at/below reorder point | `site_id`, `storeroom` | `low_stock_items[]` with `itemnum`, `curbal`, `reorderpoint`, `shortage` |
| `get_reorder_recommendations` | Prioritized reorder list | `site_id` | `recommendations[]` with `itemnum`, `suggested_qty`, `estimated_cost` |
| `check_stock_level` | Current level for one item | `item_num`, `storeroom`, `site_id` | `curbal`, `minlevel`, `reorderpoint`, `below_reorder_point` |
| `create_material_request` | Create MATRECTRANS | `item_num`, `quantity`, `location`, `site_id` | Receipt transaction. **Role: technician** |
| `transfer_inventory` | Move stock between storerooms | `item_num`, `from_storeroom`, `to_storeroom`, `quantity`, `site_id` | Transfer record. **Role: supervisor** |

---

### 5.6 Purchasing (4 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `create_purchase_order` | Create PO | `vendor`, `items[]`, `site_id` | `ponum`, PO record. **Role: manager** |
| `get_purchase_order` | Full PO details | `ponum`, `site_id` | PO fields including `poline[]` |
| `receive_items` | Record PO receipts | `ponum`, `site_id`, `received_lines[]` | Receipt record. **Role: supervisor** |
| `get_vendor_performance` | Vendor analytics | `vendor_id`, `period_months` | `on_time_pct`, `total_orders`, `avg_delivery_days`, `quality_issues` |

---

### 5.7 Labor & Locations (6 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `list_labor` | Available technicians | `site_id`, `craft`, `status` | `labor[]` with `laborcode`, `craft`, `utilization_pct` |
| `list_crews` | Maintenance crews | `site_id` | `crews[]` with `crewid`, `supervisor`, `members[]` |
| `get_labor_utilization` | Utilization metrics | `site_id`, `labor_code`, `period_days` | `utilization_pct`, `hours_worked`, `hours_available` |
| `list_locations` | Operational locations | `site_id`, `parent_location`, `location_type` | `locations[]` with `location`, `description`, `type` |
| `get_location` | Location details + assets | `location`, `site_id` | Location fields + `assets[]` |
| `get_location_hierarchy` | Location tree | `site_id`, `root_location` | Hierarchical `children[]` tree |

---

### 5.8 AI Intelligence (5 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `nl_to_oslc_query` | Natural language → OSLC query | `natural_language_query`, `object_structure`, `dry_run` | `oslc_where`, `oslc_select`, optional `results[]` |
| `detect_asset_anomalies` | Statistical anomaly detection | `asset_num`, `site_id`, `lookback_days` | `anomalies[]` with `metric`, `value`, `sigma_deviation` |
| `suggest_root_cause` | AI root cause analysis | `asset_num`, `site_id`, `failure_description` | `root_causes[]` with `cause`, `confidence_pct`, `evidence` |
| `summarize_asset_health` | Health score (0–100) | `asset_num`, `site_id` | `health_score`, `status`, `key_issues[]`, `recommendations[]` |
| `search_maximo_knowledge` | Semantic doc search | `query`, `doc_type` | `results[]` with `passage`, `source`, `relevance_score` |

> **Note:** `search_maximo_knowledge` requires `chromadb` and `sentence-transformers` installed.

---

### 5.9 Reporting (4 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `get_maintenance_kpi_dashboard` | Full KPI dashboard | `site_id`, `period_months` | `mttr`, `mtbf`, `pm_compliance_pct`, `wo_backlog`, `costs`. **Role: manager** |
| `export_workorders_excel` | Excel export | `site_id`, `filters`, `max_records` | `filename`, `file_b64` (base64 `.xlsx`) |
| `export_asset_report_pdf` | PDF export | `site_id`, `asset_group`, `max_records` | `filename`, `file_b64` (base64 `.pdf`) |
| `generate_carbon_table` | IBM Carbon HTML table | `object_structure`, `data[]`, `columns[]` | `html` string, `record_count` |

> `columns` format: `[{"key": "wonum", "header": "Work Order #"}, ...]`

---

### 5.10 Admin (4 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `list_users` | List Maximo users | `site_id` | `users[]` with `personid`, `status`, `groups[]`. **Role: admin** |
| `get_user` | User details | `user_id` | User profile fields. **Role: manager** |
| `list_security_groups` | Security group list | — | `groups[]` with `groupname`, `member_count`. **Role: admin** |
| `query_audit_log` | MCP audit trail | `tool_name`, `user_id`, `date_from`, `date_to`, `limit` | `entries[]` with tool, user, timestamp, duration. **Role: manager** |

---

### 5.11 Schema & Dev (5 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `list_object_structures` | List all Maximo OSes | `filter_keyword`, `include_custom` | `object_structures[]` with `name`, `description` |
| `get_schema_details` | Field schema for an OS | `object_structure`, `include_relationships` | `fields[]` with `name`, `type`, `required` |
| `validate_oslc_query` | Validate OSLC syntax | `object_structure`, `where_clause`, `select_clause` | `valid`, `parsed_where`, `sample_results[]` |
| `generate_api_code` | Code generation | `object_structure`, `operation`, `language` | `code` (Python, JavaScript, curl, SQL) |
| `build_custom_object_structure` | Create custom OS | `name`, `base_object`, `fields[]` | New OS registration. **Role: admin** |

---

### 5.12 Integrations (4 tools)

| Tool | Description | Key Inputs | Output |
|---|---|---|---|
| `subscribe_to_event` | Register event listener | `event_type`, `callback_url`, `filter_conditions` | Subscription record. **Role: admin** |
| `list_event_subscriptions` | Active listeners | — | `subscriptions[]` |
| `ingest_iot_alert` | SCADA/IoT → Maximo WO | `asset_num`, `sensor_type`, `reading_value`, `threshold`, `site_id` | Created WO or existing WO reference. **Role: technician** |
| `trigger_webhook` | Test fire a webhook | `event_type`, `payload` | Delivery confirmation. **Role: admin** |

---

## 6. Operations & Deployment Guide

### 6.1 Local Setup

```bash
# 1. Clone
git clone https://github.com/your-org/maximo-enterprise-mcp.git
cd maximo-enterprise-mcp

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
.\.venv\Scripts\activate         # Windows

# 3. Install dependencies (core only — no AI deps)
pip install fastmcp httpx pydantic pydantic-settings redis \
            python-jose passlib openpyxl reportlab PyYAML \
            python-dateutil openai

# OR install everything including AI/vector search:
pip install -r requirements.txt

# 4. Configure environment
cp .env .env.local
# Edit MAXIMO_URL, MAXIMO_USERNAME, MAXIMO_PASSWORD

# 5. Verify server starts
python server.py --test

# 6. Start in stdio mode (for Claude/Cursor)
python server.py

# 7. Start in HTTP mode (for testing or web clients)
python server.py --http --port 8080
```

---

### 6.2 Production Setup — Docker Compose

```bash
# 1. Build and start the full stack (MCP server + Redis)
docker compose up -d

# 2. Check status
docker compose ps
docker compose logs -f maximo-mcp

# 3. Stop
docker compose down

# 4. Stop and remove volumes (full reset)
docker compose down -v
```

The Docker stack includes:
- `maximo-enterprise-mcp` container (Python 3.11-slim, port 8080)
- `maximo-mcp-redis` container (Redis 7 Alpine, port 6379, AOF persistence)

---

### 6.3 Environment Variables Reference

| Variable | Default | Required | Description |
|---|---|---|---|
| `MAXIMO_URL` | — | ✓ | Full OSLC base URL: `http://host:9080/maximo/oslc` |
| `MAXIMO_HOST` | — | ✓ | Maximo root: `http://host:9080` |
| `AUTH_MODE` | `basic` | ✓ | `basic` \| `apikey` \| `oauth` |
| `MAXIMO_USERNAME` | — | basic | Maximo login username |
| `MAXIMO_PASSWORD` | — | basic | Maximo login password |
| `MAXIMO_API_KEY` | — | apikey | API key for `AUTH_MODE=apikey` |
| `OAUTH_TOKEN_URL` | — | oauth | Token endpoint |
| `OAUTH_CLIENT_ID` | — | oauth | OAuth client ID |
| `OAUTH_CLIENT_SECRET` | — | oauth | OAuth client secret |
| `REDIS_URL` | `redis://localhost:6379` | — | Redis connection string |
| `CACHE_TTL_SECONDS` | `300` | — | Default cache TTL |
| `CACHE_ENABLED` | `true` | — | Set `false` to disable caching |
| `TRANSPORT_MODE` | `stdio` | — | `stdio` \| `http` |
| `HTTP_PORT` | `8080` | — | HTTP mode port |
| `RBAC_ENABLED` | `true` | — | Enable role-based access control |
| `CURRENT_USER_ROLE` | `technician` | — | Active user role: `readonly` \| `technician` \| `supervisor` \| `manager` \| `admin` |
| `CURRENT_USER_ID` | `system` | — | User ID for audit trail |
| `OPENAI_API_KEY` | — | — | For LLM-enhanced NL→OSLC and RCA tools |
| `OPENAI_MODEL` | `gpt-4o-mini` | — | OpenAI model for AI tools |
| `HTTP_TIMEOUT_SECONDS` | `90` | — | OSLC request timeout |
| `HTTP_MAX_RETRIES` | `3` | — | Retry attempts on transport errors |
| `LOG_LEVEL` | `INFO` | — | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `AUDIT_LOG_FILE` | `./logs/audit.jsonl` | — | Audit log file path |

---

### 6.4 Monitoring

#### Application logs (stderr)

```bash
# Tail live logs in Docker
docker compose logs -f maximo-mcp

# Filter for errors only
docker compose logs maximo-mcp 2>&1 | grep ERROR
```

Log format:
```
2026-04-09 10:32:15,041 [INFO] core.maximo_client: Basic auth via /login succeeded
2026-04-09 10:32:16,301 [WARNING] tools.schema_dev: metadata endpoint failed (400); falling back
2026-04-09 10:32:45,120 [ERROR] tools.workorders: Connection error on GET /os/mxwo
```

#### Audit trail

Every tool call is recorded in `logs/audit.jsonl`:

```jsonl
{"ts": "2026-04-09T10:32:16Z", "tool": "create_workorder", "user": "maxadmin", "role": "admin", "inputs": {"asset_num": "P-101", "site_id": "BEDFORD"}, "success": true, "duration_ms": 342}
{"ts": "2026-04-09T10:33:01Z", "tool": "approve_workorder", "user": "maxadmin", "role": "admin", "inputs": {"wonum": "WO10042"}, "success": false, "error": "Connection error"}
```

Query the audit log via MCP:
```
"Show audit log for the last hour"
"Show all failed tool calls today"
```

Or directly:
```bash
grep '"success": false' logs/audit.jsonl | python -m json.tool
```

#### Redis monitoring

```bash
# Check Redis memory usage
docker exec maximo-mcp-redis redis-cli info memory | grep used_memory_human

# View cached keys
docker exec maximo-mcp-redis redis-cli keys "maximo:*"

# Flush cache (force refresh on next call)
docker exec maximo-mcp-redis redis-cli flushall
```

#### Health endpoint (HTTP mode)

```bash
curl http://localhost:8080/health
```

Or via MCP:
```
"Run health_check"
```

---

### 6.5 Performance Tuning

| Tuning | Action |
|---|---|
| Slow queries | Increase `CACHE_TTL_SECONDS` for read-heavy workloads |
| Maximo OOM (500 errors) | Increase Maximo JVM heap: `-Xmx4g` in `was.env` |
| Transport errors | Reduce `HTTP_MAX_KEEPALIVE_CONNECTIONS` to 5; reduce `HTTP_KEEPALIVE_EXPIRY_SECONDS` to 10 |
| Large page fetches timing out | Reduce `page_size` in tool calls; increase `HTTP_TIMEOUT_SECONDS` to 180 |
| High Redis memory | Reduce `CACHE_TTL_SECONDS`; Redis auto-evicts with LRU policy at 256MB |
| AI tools slow | Set `OPENAI_MODEL=gpt-4o-mini` (fastest); or disable with no `OPENAI_API_KEY` |

---

*Documentation generated for Maximo Enterprise MCP v2.0 — 2026-04-09*
