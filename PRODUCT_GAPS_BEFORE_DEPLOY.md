# Product Gaps Before Deploy

Security and production-readiness audit of `maximo-enterprise-mcp` against:
- the 14-point enterprise MCP runtime checklist (sections 1–3 below), and
- the 15-point pre-push / GitHub-publication checklist (sections 4–6 below).

Captured before deploying to public listings (e.g. https://github.com/mcp).

Status legend: ✅ covered · ⚠️ gap · ℹ️ note

---

## 1. Coverage summary

| # | Area | Status | Evidence |
|---|---|---|---|
| 1 | Bearer token auth (constant-time compare) | ✅ | [core/auth.py:57](core/auth.py#L57), [core/web.py:32](core/web.py#L32) |
| 1 | Hosted mode fails closed without `MCP_ACCESS_TOKEN` | ✅ | [server.py:691](server.py#L691) |
| 1 | OAuth / Basic / API-key auth to upstream Maximo | ✅ | [core/auth.py:96-136](core/auth.py#L96-L136) |
| 1 | RBAC with role hierarchy + per-tool YAML policy | ✅ | [core/rbac.py:18-145](core/rbac.py#L18-L145); 57 `@require_role` applications across 12 tool files |
| 2 | TLS verification on outbound calls | ✅ | `httpx.AsyncClient(verify=True)` — [core/maximo_client.py:112](core/maximo_client.py#L112) |
| 3 | Strict input validation (Pydantic, `extra="forbid"`) | ✅ | [core/tool_runtime.py:18](core/tool_runtime.py#L18) |
| 3 | OSLC injection guards (`safe_field_name`, `oslc_escape`) | ✅ | [core/oslc_utils.py](core/oslc_utils.py), [core/generic_oslc.py:259-267](core/generic_oslc.py#L259-L267) |
| 3 | All write tools disabled in first-deploy posture | ✅ | every `# DISABLED — write operation` block in [server.py](server.py) |
| 4 | Container runs as non-root | ✅ | [Dockerfile:24-25](Dockerfile#L24-L25) |
| 5 | Secrets via env / `.env`, no hardcoded creds | ✅ | [core/settings.py](core/settings.py); SECURITY.md references AWS Secrets Manager / Key Vault |
| 6 | Sensitive-field redaction in audit logger | ✅ | [core/audit.py:19-37](core/audit.py#L19-L37) |
| 7 | Structured JSONL audit log + query tool | ✅ | [core/audit.py](core/audit.py); `query_audit_log` MCP tool |
| 8 | Sliding-window rate limiter | ✅ | [core/rate_limiter.py](core/rate_limiter.py) |
| 10 | Tools expose intent, not raw power | ✅ | no `run_sql`; tools are `list_workorders`, `get_asset_history`, etc. |
| 11 | API gateway + edge TLS guidance documented | ✅ | [SECURITY.md](SECURITY.md), [README.md](README.md) |
| 12 | Claude tool surface constrained via FastMCP registration | ✅ | [server.py:60](server.py#L60) |

---

## 2. Gaps to close before public hosting

Each gap below blocks one or more checklist items (1, 6, 7, 8, 9, 13, 14).

### G1 — Per-request identity ✅ FIXED

**Problem.** `CURRENT_USER_ID` / `CURRENT_USER_ROLE` were process-wide env vars, so every HTTP caller looked like the same user — audit, rate limiting, and tenant isolation were all degraded.

**Resolution.**
- New module [core/identity.py](core/identity.py) with an `Identity(user_id, role, tenant_id)` dataclass on a `contextvars.ContextVar`. `resolve_identity()` falls back to env settings for stdio mode.
- New pure-ASGI `IdentityMiddleware` in [core/web.py](core/web.py) reads `X-MCP-User-Id`, `X-MCP-Tenant-Id`, `X-MCP-Role` headers and binds them into the contextvar per request. Pure-ASGI (not BaseHTTPMiddleware) to avoid Starlette's known contextvar-propagation issue with BaseHTTPMiddleware.
- Middleware order in [core/web.py](core/web.py): `BearerTokenMiddleware` outer (auth first) → `IdentityMiddleware` inner (set context) → app.
- [core/rbac.py](core/rbac.py) `_get_current_user` / `_get_current_role` now read from `resolve_identity()`.
- Rate limiter key is now `f"{tenant_id}:{user_id}"` ([core/identity.py](core/identity.py) `Identity.rate_limit_key`), so a noisy tenant cannot starve others.

**Still required for full multi-tenant posture (G6).** The middleware trusts the headers; the gateway must validate the JWT and inject these claims. Until G6 lands, only deploy behind a trusted gateway.

### G2 — Audit logger wired into every tool call ✅ FIXED

**Problem.** `core/audit.py` existed but `record()` was never called. `query_audit_log` returned an empty file.

**Resolution.** [core/rbac.py](core/rbac.py) `require_role` is the universal hook (every tool call routes through it in both stdio and HTTP modes). The decorator now emits audit on every exit path:
- successful call (with `duration_ms`, sanitized inputs via `_bind_inputs`, summarized result),
- application failure (`{"success": False, ...}`) recorded as `success=False` with the returned `error_code`,
- RBAC denial recorded with `error="RBAC_DENIED"`,
- rate-limit denial recorded with `error="RATE_LIMITED"`,
- exception path records before re-raising.

Audit emission is wrapped in try/except (`_emit_audit`) so a disk-write failure can never break a tool call.

### G3 — Output PII masking ✅ FIXED

**Problem.** Maximo responses passed straight through to the LLM. If the underlying records contained person data (LABOR, PERSON, PURCHCONTACT, USERINFO, etc.), there was no field-level redaction before the response crossed the trust boundary.

**Resolution.**
- New module [core/pii.py](core/pii.py) with a `mask_pii()` recursive walker. Matches case-insensitively against the *leaf* attribute name (so dotted Maximo names like `LABOR.EMPLOYEEEMAIL` are matched correctly). Internal bookkeeping keys starting with `_` are passed through untouched (e.g. `_duration_ms`).
- Default field set covers email, phone, government IDs (SSN/taxid/passport/driverslicense), financial (salary/wage/bank/credit-card), demographics (birthdate/gender), and *personal* address fields. Asset/site address fields (`city`, `country`, `postalcode`) are intentionally NOT masked — those refer to physical locations, not people.
- Wired in at the single response boundary [core/maximo_client.py:428](core/maximo_client.py#L428): every response that flows through `_request` is masked before return, so all four HTTP verbs (`get`/`post`/`patch`/`delete`) are covered automatically.
- New settings: `PII_MASK_ENABLED` (default `true`), `PII_MASK_FIELDS` (extra leaf names to append), `PII_MASK_VALUE` (default `"***MASKED***"`). See [.env.example](.env.example).
- Operators can disable for trusted internal deployments by setting `PII_MASK_ENABLED=false`; defaults are safe-by-default.

### G4 — CORS allowlist ✅ FIXED

**Problem.** FastAPI did not register `CORSMiddleware`. Browser MCP clients had no path in, and a permissive default added later would have leaked credentials cross-origin.

**Resolution.**
- New settings: `MCP_ALLOWED_ORIGINS` (default `[]` — deny by default), `MCP_CORS_ALLOW_CREDENTIALS`, `MCP_CORS_ALLOW_METHODS`, `MCP_CORS_ALLOW_HEADERS`.
- [core/web.py](core/web.py) registers `CORSMiddleware` only when `MCP_ALLOWED_ORIGINS` is non-empty, and adds it LAST so it sits *outside* the auth middleware — preflight `OPTIONS` requests are answered without an auth challenge.
- Settings validator rejects the well-known browser trap of `"*"` + `MCP_CORS_ALLOW_CREDENTIALS=true` at startup.
- Documented in [.env.example](.env.example) with explicit JSON-array example.

### G5 — TLS posture: mandatory edge, optional in-process ✅ FIXED

**Problem.** `uvicorn.run(...)` served plaintext, and SECURITY.md / README.md called TLS "recommended." For public listings this must be **mandatory** language, and operators with no edge proxy needed an in-process option.

**Resolution.**
- [SECURITY.md](SECURITY.md): rewritten "Hosted Mode Requirements" section. TLS, inbound auth, and CORS allowlist are now explicitly mandatory; rate-limiting / audit forwarding moved to "recommended."
- [README.md](README.md): added a top-of-section TLS notice; AWS/Azure deployment bullets changed from "Put ... in front" to "**Required:** put ... in front"; the "Public Internet" checklist is now an unconditional ALL-of list.
- New optional in-process TLS path: `MCP_SSL_CERTFILE` / `MCP_SSL_KEYFILE` settings; if both are set, [server.py:_run_http](server.py) passes them to `uvicorn.run` via `ssl_certfile` / `ssl_keyfile`. Settings validator enforces the both-or-neither invariant.
- When in-process TLS is NOT configured, `_run_http` now logs a warning at startup instructing the operator to terminate TLS at the edge.

### G6 — Inbound JWT/OIDC validation ✅ FIXED

**Problem.** Only one static `MCP_ACCESS_TOKEN`. No JWT/OIDC validation; no per-caller identity, rotation, or revocation.

**Resolution.**
- New module [core/jwt_auth.py](core/jwt_auth.py) validates inbound JWTs against the configured OIDC issuer's JWKS (signature, `iss`, `aud`, `exp`, `iat`). JWKS is cached in-memory with a configurable TTL and auto-refreshed once on a `kid` miss to handle key rotation.
- New unified `MCPAuthMiddleware` in [core/auth.py](core/auth.py) replaces both the previous `BearerTokenMiddleware` and `IdentityMiddleware`. It supports three modes via `MCP_AUTH_MODE`:
  - `static` — single shared token; identity from `X-MCP-*` headers (gateway-injected) or env defaults.
  - `jwt` — JWT required; identity from validated claims (`OIDC_USER_CLAIM`, `OIDC_ROLE_CLAIM`, `OIDC_TENANT_CLAIM`).
  - `both` — JWT tried first; falls back to static (useful for ops/CI).
- Role claim accepts string or list; on a list, the highest-privilege role from the project's RBAC hierarchy wins.
- Settings additions: `MCP_AUTH_MODE`, `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL` (auto-derived from issuer if blank), `OIDC_ALGORITHMS`, `OIDC_JWKS_CACHE_TTL_SECONDS`, `OIDC_USER_CLAIM`, `OIDC_ROLE_CLAIM`, `OIDC_TENANT_CLAIM`. See [.env.example](.env.example).
- Settings validator now requires `OIDC_ISSUER` + `OIDC_AUDIENCE` whenever `MCP_AUTH_MODE` is `jwt` or `both`. `server.py` only requires `MCP_ACCESS_TOKEN` when the mode actually uses it.
- New runtime dependency: `pyjwt[crypto]>=2.8.0` (added to [requirements.txt](requirements.txt)). Imported lazily so static-mode deployments don't pull crypto bindings unless needed.

**Defaults are backward-compatible.** `MCP_AUTH_MODE` defaults to `static`, so existing deployments behave exactly as before.

### G7 — Multi-stage Dockerfile, no compilers / no curl in runtime ✅ FIXED

**Problem.** Single-stage build kept `gcc`/`g++`/`curl` in the runtime image and used `curl` for the healthcheck.

**Resolution.**
- [Dockerfile](Dockerfile) is now multi-stage:
  - **builder** — installs `gcc`/`g++`, builds and installs all Python deps + the package.
  - **runtime** — `python:3.11-slim`, copies only the installed `site-packages`, console scripts, and `/app` source from the builder. No compilers, no curl.
- Healthcheck rewritten to use `python -c "import urllib.request; ..."` against `/healthz`, so the runtime image no longer needs `curl`.
- Smaller attack surface and image size; nothing functional changes for users.

### G8 — Pluggable audit sinks + rotation ✅ FIXED

**Problem.** `./logs/audit.jsonl` lived on the container's local filesystem with no rotation, no SIEM integration, and was lost on container restart.

**Resolution.**
- New module [core/audit_sinks.py](core/audit_sinks.py) defines an `AuditSink` ABC and three implementations:
  - `FileSink` — JSONL with size-based rotation via `RotatingFileHandler`. Default 50 MB × 5 backups, both configurable.
  - `StdoutSink` — JSONL to stdout for cloud-native deployments where the container runtime / sidecar (Fluent Bit, Vector, CloudWatch Agent, Datadog Agent) captures and forwards to the SIEM. No local disk dependency.
  - `CompositeSink` — fan-out; one sink failing doesn't block the others.
- [core/audit.py](core/audit.py) `AuditLogger` now delegates persistence to a sink. Constructor accepts either `log_file=` (backward compatible — builds a `FileSink`) or `sink=` (any `AuditSink`). `query()` continues to work when a file is in the sink chain; returns `[]` with a debug log otherwise.
- New settings: `AUDIT_SINK` (`file` | `stdout` | `both`, default `file`), `AUDIT_FILE_MAX_BYTES` (default 50 MB), `AUDIT_FILE_BACKUP_COUNT` (default 5). See [.env.example](.env.example).
- Operators with bespoke needs (S3, Kafka, Splunk HEC, ...) subclass `AuditSink` and inject it via the `AuditLogger(sink=...)` constructor.
- Backward compatibility: existing `AuditLogger("./logs/audit.jsonl")` callers (test suite, ad-hoc scripts) still work — `FileSink` is built lazily.

### G9 — Dead duplicate auth middleware ✅ FIXED

**Resolution.** Removed as part of G6: both the dead `MCPBearerMiddleware` (in [core/auth.py](core/auth.py)) and the live `BearerTokenMiddleware` + `IdentityMiddleware` (in [core/web.py](core/web.py)) are gone. They are replaced by a single unified `MCPAuthMiddleware` that owns inbound auth and identity binding.

### G10 — `nl_to_oslc_query` LLM-output validation ✅ FIXED

**Problem.** `nl_to_oslc_query` lets an LLM produce free-text OSLC clauses (`oslc_where`, `oslc_select`, `oslc_orderBy`). The pattern-parser path was already injection-safe (it routes string values through `oslc_escape` and uses fixed field names), but the LLM-enhancement path returned the model's output verbatim — both back to the caller as `ready_to_use_params` and into `client.build_oslc_query` when `dry_run=True`.

**Resolution.**
- New validators in [core/oslc_utils.py](core/oslc_utils.py):
  - `validate_oslc_where(clause)` — tokenizes against a strict whitelist (identifiers via `safe_field_name`; operators `= != < > <= >=`; keywords `and / or / not / in / true / false / null`; numeric literals; properly-quoted strings; balanced `()` and `[]`). **Structural check**: every comparison operator must be preceded by a field identifier — blocks tautology injections like `1=1` or `"x"="x"` even though each individual token would tokenize cleanly. Length-capped at 1000 chars.
  - `validate_oslc_select(clause)` — comma-split + per-field `safe_field_name`. Length-capped at 500 chars.
  - `validate_oslc_orderby(clause)` — single field, optional `+`/`-` prefix; rejects commas (which would smuggle a multi-field list). Length-capped at 200 chars.
  - All three reject `;`, `--`, `/* */`, and `\x00` outright.
- [tools/ai_intelligence.py:_llm_enhance_oslc](tools/ai_intelligence.py) now validates every LLM-produced clause before returning it. On failure: discard the LLM result, fall back to the (already-safe) pattern result, and surface `llm_validation_error` to the caller for observability.
- New adversarial coverage in [tests/test_oslc_validators.py](tests/test_oslc_validators.py): 50 cases — 24 valid clauses must round-trip unchanged; 26 attacks (semicolon, SQL comments, null bytes, unbalanced brackets, tautologies, smuggled commas, exotic identifier characters) must raise. All passing.

---

## 3. Suggested fix order

1. ~~**G1 + G2 together**~~ ✅ shipped — see resolution notes above.
2. ~~**G6**~~ ✅ shipped — JWT/OIDC validation with JWKS caching, plus `static`/`jwt`/`both` modes. G9 closed in the same change.
3. ~~**G3**~~ ✅ shipped — PII masking applied at the Maximo response boundary; configurable via `PII_MASK_*` settings.
4. ~~**G4 + G5**~~ ✅ shipped — CORS allowlist (deny-by-default) + mandatory edge-TLS docs + optional in-process TLS via `MCP_SSL_*`.
5. ~~**G8**~~ ✅ shipped — pluggable audit sinks (`file` / `stdout` / `both`) with rotation; clean ABC for cloud-native sink implementations.
6. ~~**G7 + G10**~~ ✅ shipped — multi-stage Dockerfile (no compilers, no curl, python-stdlib healthcheck) and LLM-output validators for `nl_to_oslc_query` with 50-case adversarial test suite. (G9 already closed under G6.)

Items 1–4 are the minimum bar for "https://github.com/mcp"-style public exposure. Items 5–6 are required for SOC2/GDPR posture.

---

## 4. Pre-push / GitHub-publication coverage summary

Audit of the 15-point repo-and-supply-chain checklist applied to the working tree.

| # | Area | Status | Evidence |
|---|---|---|---|
| 2 | `.env`, `.mcp.json`, `.cursor/mcp.json`, `*.jsonl`, `logs/` excluded from git | ✅ | [.gitignore](.gitignore) |
| 2 | `.dockerignore` excludes secrets and dev artefacts from image | ✅ | [.dockerignore](.dockerignore) |
| 2 | No hardcoded secrets in source | ✅ | grep `password\|api_key\|secret\|token` shows only field-name constants and `settings.X` reads |
| 3 | No internal/private hostnames in code | ✅ | only `localhost:6379` (default Redis) and intra-container healthchecks remain |
| 4 | No `eval`, `exec`, `subprocess`, `os.system`, `shell=True` | ✅ | grep returned no matches in [core/](core/) or [tools/](tools/) |
| 4 | No `/debug`, `/test-*`, `/admin/*` HTTP routes | ✅ | grep against [server.py](server.py) and [core/web.py](core/web.py) |
| 4 | `DEBUG_HTTP` defaults to `False` | ✅ | [core/settings.py:91](core/settings.py#L91) |
| 5 | CI workflow exists | ✅ | [.github/workflows/ci.yml](.github/workflows/ci.yml) — ruff + pytest |
| 9 | Container runs non-root, slim base image | ✅ | [Dockerfile:1,24-25](Dockerfile#L1) |
| 10 | Audit log redacts sensitive keys | ✅ | [core/audit.py:19-37](core/audit.py#L19-L37) |
| 10 | HTTP client log helper redacts sensitive keys + truncates payloads | ✅ | [core/maximo_client.py:22-38](core/maximo_client.py#L22-L38) |
| 12 | Apache 2.0 LICENSE present | ✅ | [LICENSE](LICENSE) |
| 13 | No hardcoded tenant IDs in business logic | ✅ | "BEDFORD" appears only in docstrings, tests, and the RAG knowledge corpus |

---

## 5. Pre-push / GitHub-publication gaps

### G11 — Dependency hygiene + Dependabot + CodeQL ✅ MOSTLY FIXED

**Problem.** Loose `>=` pins, no Dependabot, no CodeQL.

**Resolution.**
- New [.github/dependabot.yml](.github/dependabot.yml): weekly updates for `pip`, `github-actions`, and `docker`. Runtime/dev Python deps grouped to keep PR volume sane.
- New [.github/workflows/codeql.yml](.github/workflows/codeql.yml): Python analysis on push, PR, and weekly cron with the `security-and-quality` query pack.
- `pip-audit` added to [.github/workflows/ci.yml](.github/workflows/ci.yml) — every push fails the build on a CVE in `requirements.txt`.

**Still deferred.** Hash-pinned lock file (`pip-compile --generate-hashes`) is operator-side: requires running `pip-tools` against a clean environment to produce a `requirements.lock`. Tracked in [docs/REPO_HARDENING.md](docs/REPO_HARDENING.md) under "Pre-publication audit." Until lock is in place, CI uses pip-audit for the safety net.

### G12 — CI security scanning ✅ FIXED

**Problem.** CI ran `ruff` + `pytest` only — no secret scan, no SAST, no dependency audit, no container scan.

**Resolution.** New [.github/workflows/security.yml](.github/workflows/security.yml) wires three jobs alongside the new CodeQL workflow (G11):
- **gitleaks** — committed-secret scan with full git history (`fetch-depth: 0`).
- **pip-audit** — Python dependency CVEs against `requirements.txt`.
- **trivy** — builds the production Docker image and scans it for `CRITICAL`/`HIGH` CVEs (ignore-unfixed); SARIF uploaded to the GitHub security tab.

All three plus CodeQL run on push, PR, and a weekly cron. Pre-merge gating (the "Required status checks" list) is enforced in [docs/REPO_HARDENING.md](docs/REPO_HARDENING.md).

### G13 — Scratch scripts removed + defensive .gitignore ✅ FIXED

**Resolution.** Deleted `scripts/_qa_diag2.py`, `_qa_diag_test.py`, `_qa_url_test.py`, `_tmp_describe_test.py`, and `run_mcp_tool_tests copy.py`. The two legitimate scripts (`run_mcp_tool_tests.py`, `test_maximo_connection.py`) remain. Added defensive globs to [.gitignore](.gitignore): `_qa_*.py`, `_tmp_*.py`, `* copy.py`, `* copy.*.py`, `*.tmp.py` — so any future scratch file is ignored by default.

### G14 — Environment separation pattern ✅ FIXED

**Resolution.**
- New setting `MAXIMO_ENV` ([core/settings.py](core/settings.py), default `dev`) — free-form deployment label, recommended values `dev|staging|prod`.
- Stamped into every audit record by [core/audit.py](core/audit.py) (new `env` field on each entry) and surfaced on `/healthz` by [core/web.py](core/web.py) so an operator can confirm at a glance which Maximo a container is talking to.
- New "Environment Separation" subsection in [README.md](README.md) documents the one-image / three-deployments / three-secret-stores pattern with an explicit "never reuse a token across environments" warning.
- [.env.example](.env.example) now leads with the `MAXIMO_ENV` block so it is the first thing operators set.

### G15 — README "Responsible Use" section ✅ FIXED

**Resolution.** Added a "Responsible Use" section near the top of [README.md](README.md) covering: gateway / OIDC requirement for hosted mode, mandatory edge-TLS, write tools staying disabled until reviewed, `MAXIMO_ENV` discipline, and pointers to [SECURITY.md](SECURITY.md) and this document. Visible before the architecture diagram so a casual reader cannot miss it.

### G16 — Repo-hardening checklist ✅ FIXED

**Resolution.** New [docs/REPO_HARDENING.md](docs/REPO_HARDENING.md) codifies all GitHub-side controls: visibility decision (with reviewer + date capture), branch protection on `main` (PR review, required CI checks including `gitleaks` / `pip-audit` / `trivy` / `CodeQL`, no force-push, no direct push, linear history), code-scanning toggles (secret scanning + push protection + Dependabot alerts + security updates + Dependabot version updates), token / secret hygiene, tag protection, and a pre-publication audit gate that explicitly references G1–G18.

### G17 — `/v1/tools` discovery gate ✅ FIXED

**Resolution.** New setting `MCP_DISCOVERY_ENABLED` (default `true` for backward compatibility). When `false`, [core/web.py](core/web.py) returns 404 for `/v1/tools`, `/v1/providers/openai-tools`, `/v1/providers/gemini-tools`, and `/v1/providers/grok-tools`. Operators flip this to `false` in production unless paired with per-caller OIDC tokens (G6) so a leaked bearer token cannot enumerate the full tool surface. Documented in [.env.example](.env.example) and [docs/REPO_HARDENING.md](docs/REPO_HARDENING.md).

### G18 — In-tree audit log truncation ✅ FIXED

**Resolution.** Truncated both `logs/audit.jsonl` (3919 bytes → 0) and `scripts/logs/audit.jsonl` (779 bytes → 0). `.gitignore` (covers `logs/` and `*.jsonl`) and `.dockerignore` (covers `logs/`) already prevent them from shipping; truncation removes the last on-disk copy with real test data. Verifying no historical commit ever included them (`git log --all -- logs/`) is part of the pre-publication audit in [docs/REPO_HARDENING.md](docs/REPO_HARDENING.md).

---

## 6. Pre-push checklist (run before every `git push origin main`)

Concrete gates derived from the above. Each item maps to one or more gaps.

- [ ] `git status` is clean; `.env`, `.mcp.json`, `.cursor/mcp.json`, `logs/`, `*.jsonl` not staged. (G18)
- [ ] `git ls-files | grep -E '^\.env$|\.mcp\.json$|audit\.jsonl$'` returns nothing. (#2)
- [ ] No hardcoded secrets: `gitleaks detect --source . --no-git` exits clean. (#2)
- [ ] No internal hostnames: grep for prod hostnames returns nothing. (#3)
- [ ] No debug routes: grep `/debug\|/test-\|/admin/` in `core/` and `server.py` returns nothing. (#4)
- [ ] `_qa_*.py`, `_tmp_*.py`, `* copy.py` removed from `scripts/`. (G13)
- [ ] CI green, including `pip-audit` and `gitleaks` jobs. (G11, G12)
- [ ] Lock file regenerated and committed if `requirements.txt` changed. (G11)
- [ ] All write tools still `# DISABLED` in [server.py](server.py) (or explicitly enabled with reviewer sign-off).
- [ ] Audit log truncated locally; not bundled in any release artefact. (G18)
- [ ] If publishing, repo visibility decision made and recorded in `docs/REPO_HARDENING.md`. (G16)
- [ ] Tenant isolation verified: no caller-supplied identity short-circuits server-derived `tenant_id` (depends on G1 landing). (#13)

