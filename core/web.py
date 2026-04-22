"""
Hosted HTTP app for MCP SSE and REST tool execution.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from core.auth import MCPAuthMiddleware
from core.response_utils import error_response
from core.settings import get_settings
from core.tool_catalog import bind_runtime
from core.tool_runtime import execute_tool, get_parameters_schema


def extract_mcp_asgi_app(mcp: Any) -> Any:
    for method_name in ("http_app", "sse_app", "get_asgi_app", "_build_sse_app"):
        method = getattr(mcp, method_name, None)
        if callable(method):
            app = method()
            if app is not None:
                return app
    raise RuntimeError("FastMCP ASGI app extraction failed; hosted HTTP mode is unavailable.")


def build_openai_tools(bound_catalog: Dict[str, Any]) -> list[Dict[str, Any]]:
    tools = []
    for spec in bound_catalog.values():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.summary,
                    "parameters": get_parameters_schema(spec),
                },
            }
        )
    return tools


def build_gemini_tools(bound_catalog: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "function_declarations": [
            {
                "name": spec.name,
                "description": spec.summary,
                "parameters": get_parameters_schema(spec),
            }
            for spec in bound_catalog.values()
        ]
    }


def create_http_app(mcp: Any, server_module: ModuleType, token: Optional[str]) -> FastAPI:
    bound_catalog = bind_runtime(server_module)
    app = FastAPI(
        title="Maximo Enterprise MCP API",
        version="1.0.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    settings = get_settings()
    app.add_middleware(
        MCPAuthMiddleware,
        mode=settings.MCP_AUTH_MODE,
        static_token=token,
        default_role=settings.CURRENT_USER_ROLE,
    )
    # CORS is added LAST so it sits OUTERMOST: preflight OPTIONS requests
    # are answered before they reach the auth middleware. Empty allowlist
    # means CORS is not registered at all (deny-by-default).
    if settings.MCP_ALLOWED_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.MCP_ALLOWED_ORIGINS,
            allow_credentials=settings.MCP_CORS_ALLOW_CREDENTIALS,
            allow_methods=settings.MCP_CORS_ALLOW_METHODS,
            allow_headers=settings.MCP_CORS_ALLOW_HEADERS,
        )

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        base = await server_module.health_check()
        # Surface the deployment label so an operator can confirm at a
        # glance which Maximo this container is talking to.
        if isinstance(base, dict):
            base.setdefault("env", settings.MAXIMO_ENV)
        return base

    def _require_discovery() -> None:
        if not settings.MCP_DISCOVERY_ENABLED:
            raise HTTPException(
                status_code=404,
                detail="Tool discovery is disabled (MCP_DISCOVERY_ENABLED=false).",
            )

    @app.get("/v1/tools")
    async def list_tools() -> Dict[str, Any]:
        _require_discovery()
        return {
            "tools": [
                {
                    "name": spec.name,
                    "category": spec.category,
                    "stability": spec.stability,
                    "description": spec.summary,
                    "parameters": get_parameters_schema(spec),
                }
                for spec in bound_catalog.values()
            ]
        }

    @app.post("/v1/tools/{tool_name}")
    async def invoke_tool(tool_name: str, request: Request) -> Dict[str, Any]:
        spec = bound_catalog.get(tool_name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown tool '{tool_name}'")
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        request_id = request.headers.get("x-request-id") or str(uuid4())
        try:
            return await execute_tool(spec, body or {}, request_id=request_id)
        except Exception as exc:
            return error_response(
                f"{type(exc).__name__}: {exc}",
                code="EXECUTION_ERROR",
                request_id=request_id,
                metadata={"tool_name": tool_name},
            )

    @app.get("/v1/providers/openai-tools")
    async def openai_tools() -> Dict[str, Any]:
        _require_discovery()
        return {"tools": build_openai_tools(bound_catalog)}

    @app.get("/v1/providers/gemini-tools")
    async def gemini_tools() -> Dict[str, Any]:
        _require_discovery()
        return build_gemini_tools(bound_catalog)

    @app.get("/v1/providers/grok-tools")
    async def grok_tools() -> Dict[str, Any]:
        _require_discovery()
        return {"tools": build_openai_tools(bound_catalog)}

    app.mount("/", extract_mcp_asgi_app(mcp))
    return app
