"""
core/rbac.py — Role-based access control decorator for MCP tools.

Roles are defined in config/rbac_policies.yaml. The active caller identity
is resolved through core.identity.resolve_identity(), which reads from a
per-request contextvar in HTTP/SSE mode and falls back to the env-level
CURRENT_USER_ID / CURRENT_USER_ROLE settings for stdio mode.

The require_role decorator is the universal hook for every tool call and
therefore also the place where audit records are emitted — for successful
calls, RBAC denials, rate-limit denials, and exceptions alike.
"""

import functools
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Path to RBAC policy file
POLICY_FILE = Path(__file__).parent.parent / "config" / "rbac_policies.yaml"

# Role hierarchy (each role inherits permissions of roles below it)
ROLE_HIERARCHY: dict[str, List[str]] = {
    "admin": ["manager", "supervisor", "technician", "readonly"],
    "manager": ["supervisor", "technician", "readonly"],
    "supervisor": ["technician", "readonly"],
    "technician": ["readonly"],
    "readonly": [],
}


def _load_policies() -> dict[str, List[str]]:
    """
    Load tool-to-role mapping from rbac_policies.yaml.
    Returns dict: {tool_name: [allowed_roles]}.
    Falls back to an empty dict (all tools accessible) if file missing.
    """
    if not POLICY_FILE.exists():
        logger.warning("RBAC policy file not found at %s; all tools accessible.", POLICY_FILE)
        return {}
    try:
        import yaml  # type: ignore
        with open(POLICY_FILE, "r") as f:
            data = yaml.safe_load(f) or {}
        return data.get("tools", {})
    except Exception as exc:
        logger.error("Failed to load RBAC policies: %s", exc)
        return {}


_policies: Optional[dict[str, List[str]]] = None


def get_policies() -> dict[str, List[str]]:
    global _policies
    if _policies is None:
        _policies = _load_policies()
    return _policies


def _has_role(user_role: str, required_role: str) -> bool:
    """
    Check if user_role satisfies required_role via the role hierarchy.
    A role satisfies itself plus all roles lower in the hierarchy.
    """
    if user_role == required_role:
        return True
    inherited = ROLE_HIERARCHY.get(user_role, [])
    return required_role in inherited


def _get_current_role() -> str:
    from core.identity import resolve_identity
    return resolve_identity().role


def _get_current_user() -> str:
    from core.identity import resolve_identity
    return resolve_identity().user_id


def _bind_inputs(fn: Callable, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Map positional + keyword args back to parameter names for audit input capture."""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except Exception:  # noqa: BLE001
        out: Dict[str, Any] = {f"arg{i}": v for i, v in enumerate(args)}
        out.update(kwargs)
        return out


async def _emit_audit(
    fn_name: str,
    inputs: Dict[str, Any],
    result: Any,
    user_id: str,
    duration_ms: int,
    success: bool,
    error: Optional[str] = None,
) -> None:
    """Best-effort audit emission. Audit failures must never break tool calls."""
    try:
        from core.audit import get_audit_logger
        await get_audit_logger().record(
            tool_name=fn_name,
            input_params=inputs,
            result=result,
            user_id=user_id,
            duration_ms=duration_ms,
            success=success,
            error=error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Audit emission failed for tool=%s user=%s: %s", fn_name, user_id, exc)


def require_role(minimum_role: str) -> Callable:
    """
    Decorator that enforces a minimum role requirement AND per-user rate limiting.

    Usage:
        @require_role("supervisor")
        async def approve_workorder(...):
            ...

    Rate limiting is always applied (even when RBAC_ENABLED=false).
    If RBAC_ENABLED=false, the role check itself is skipped.
    The tool's policy in rbac_policies.yaml takes precedence over the decorator argument.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            from core.identity import resolve_identity
            from core.rate_limiter import get_rate_limiter
            from core.settings import get_settings

            settings = get_settings()
            identity = resolve_identity()
            tool_name = fn.__name__
            inputs = _bind_inputs(fn, args, kwargs)

            # ── Rate limiting (always active, keyed by tenant:user) ──────────
            if not await get_rate_limiter().check(identity.rate_limit_key):
                logger.warning(
                    "Rate limit exceeded: key=%s tool=%s", identity.rate_limit_key, tool_name
                )
                result = {
                    "success": False,
                    "error": (
                        f"Rate limit exceeded. Maximum {settings.RATE_LIMIT_PER_MINUTE} "
                        "calls per minute allowed."
                    ),
                    "error_code": "RATE_LIMITED",
                }
                await _emit_audit(
                    tool_name, inputs, result, identity.user_id,
                    duration_ms=0, success=False, error="RATE_LIMITED",
                )
                return result

            # ── RBAC ─────────────────────────────────────────────────────────
            if settings.RBAC_ENABLED:
                policies = get_policies()
                required_roles = policies.get(tool_name, [minimum_role])
                if not any(_has_role(identity.role, req) for req in required_roles):
                    logger.warning(
                        "RBAC DENIED: user=%s role=%s tried tool=%s (requires one of %s)",
                        identity.user_id, identity.role, tool_name, required_roles,
                    )
                    result = {
                        "success": False,
                        "error": (
                            f"Access denied: tool '{tool_name}' requires role in "
                            f"{required_roles}, but current role is '{identity.role}'."
                        ),
                        "error_code": "RBAC_DENIED",
                    }
                    await _emit_audit(
                        tool_name, inputs, result, identity.user_id,
                        duration_ms=0, success=False, error="RBAC_DENIED",
                    )
                    return result

            # ── Execute + audit ──────────────────────────────────────────────
            start = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                duration_ms = int((time.monotonic() - start) * 1000)
                await _emit_audit(
                    tool_name, inputs,
                    result={"error": str(exc), "error_type": type(exc).__name__},
                    user_id=identity.user_id,
                    duration_ms=duration_ms,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

            duration_ms = int((time.monotonic() - start) * 1000)
            # A tool returning {"success": False, ...} is still a completed call;
            # we treat it as an application-level failure for audit accuracy.
            success_flag = True
            err_msg: Optional[str] = None
            if isinstance(result, dict) and result.get("success") is False:
                success_flag = False
                err_msg = str(result.get("error") or result.get("error_code") or "unknown")
            await _emit_audit(
                tool_name, inputs, result, identity.user_id,
                duration_ms=duration_ms, success=success_flag, error=err_msg,
            )
            return result

        return wrapper
    return decorator
