import pytest

# starlette 0.49+ TestClient does `httpx.Response` at module-import time,
# which fails when the resolved starlette/httpx pair is mismatched. Skip the
# whole module rather than break collection. Once the starlette<2.0.0 widening
# (see Dependabot PR) lands and dep resolution stabilizes, this can be removed.
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from unittest.mock import AsyncMock, patch  # noqa: E402


async def dummy_asgi_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_bound_tool_catalog_matches_declared_surface():
    import server
    from core.tool_catalog import ACTIVE_TOOL_COUNT, bind_runtime

    bound = bind_runtime(server)
    assert len(bound) == ACTIVE_TOOL_COUNT
    assert "list_assets" in bound
    assert "list_workorders" in bound


@pytest.mark.asyncio
async def test_execute_tool_adds_request_id():
    import server
    from core.tool_catalog import bind_runtime
    from core.tool_runtime import execute_tool

    mock_cache = AsyncMock()
    mock_cache.get_or_fetch.return_value = (
        {
            "data": [{"assetnum": "P-101", "description": "Pump"}],
            "totalCount": 1,
            "object_structure": "mxasset",
            "entity": "asset",
            "filters": {},
            "_duration_ms": 5,
        },
        False,
    )

    with patch("tools.assets.get_cache", return_value=mock_cache):
        spec = bind_runtime(server)["list_assets"]
        result = await execute_tool(spec, {"site_id": "BEDFORD"}, request_id="req-123")

    assert result["success"] is True
    assert result["metadata"]["request_id"] == "req-123"


def test_http_app_protects_tool_routes_but_not_health():
    import server
    from core import web

    health_payload = {"success": True, "data": {"status": "ok"}, "metadata": {"duration_ms": 1}}

    with patch.object(web, "extract_mcp_asgi_app", return_value=dummy_asgi_app), \
         patch.object(server, "health_check", AsyncMock(return_value=health_payload)):
        app = web.create_http_app(server.mcp, server, "secret-token")
        client = TestClient(app)

        health = client.get("/healthz")
        unauthorized = client.get("/v1/tools")
        authorized = client.get(
            "/v1/tools",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert health.status_code == 200
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert "tools" in authorized.json()
