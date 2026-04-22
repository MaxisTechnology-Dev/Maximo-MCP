"""
core/maximo_client.py — Async OSLC/REST client for IBM Maximo.
Supports API key, Basic Auth, and OAuth 2.0 authentication.
All tool modules must use this client exclusively.
"""

import asyncio
import base64
import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin

import httpx

from core.pii import mask_response
from core.settings import get_settings

logger = logging.getLogger(__name__)

# Fields that must never appear in logs
SENSITIVE_FIELDS = {"api_key", "password", "token", "secret", "apikey", "Authorization"}


def _sanitize(data: Dict) -> Dict:
    """Strip sensitive keys from a dict before logging."""
    return {k: "***" if k.lower() in {f.lower() for f in SENSITIVE_FIELDS} else v
            for k, v in data.items()}


def _truncate(val: Any, limit: int = 500) -> Any:
    try:
        s = str(val)
    except Exception:
        return val
    if len(s) <= limit:
        return val
    return s[:limit] + "...(truncated)"


class MaximoAuthError(Exception):
    """Raised when authentication fails."""


class MaximoAPIError(Exception):
    """Raised when the Maximo API returns an error response."""

    def __init__(self, message: str, status_code: int = 0, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class MaximoClient:
    """
    Async HTTP client for IBM Maximo OSLC/REST APIs.

    Usage:
        async with MaximoClient() as client:
            data = await client.get("/mxasset", params={"oslc.where": "siteid=\"BEDFORD\""})
    """

    def __init__(self):
        self.settings = get_settings()
        self._client: Optional[httpx.AsyncClient] = None
        self._oauth_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._cookie_jar: Dict[str, str] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> "MaximoClient":
        await self._init_client()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def _init_client(self):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        # Timeouts (Maximo can be slow on heavy OSLC queries)
        read_timeout = float(getattr(self.settings, "HTTP_READ_TIMEOUT_SECONDS", 0) or self.settings.HTTP_TIMEOUT_SECONDS)
        timeout = httpx.Timeout(
            connect=float(getattr(self.settings, "HTTP_CONNECT_TIMEOUT_SECONDS", 3.0) or 3.0),
            read=read_timeout if read_timeout else 10.0,
            write=float(getattr(self.settings, "HTTP_WRITE_TIMEOUT_SECONDS", 30.0) or 30.0),
            pool=float(getattr(self.settings, "HTTP_POOL_TIMEOUT_SECONDS", 10.0) or 10.0),
        )
        _max_conn = getattr(self.settings, "HTTP_MAX_CONNECTIONS", 50)
        _max_ka = getattr(self.settings, "HTTP_MAX_KEEPALIVE_CONNECTIONS", 10)
        _ka_expiry = getattr(self.settings, "HTTP_KEEPALIVE_EXPIRY_SECONDS", 20.0)
        limits = httpx.Limits(
            max_connections=int(_max_conn) if _max_conn is not None else 50,
            max_keepalive_connections=int(_max_ka) if _max_ka is not None else 10,
            keepalive_expiry=float(_ka_expiry) if _ka_expiry is not None else 20.0,
        )

        if self.settings.AUTH_MODE == "apikey":
            headers["apikey"] = self.settings.MAXIMO_API_KEY or ""
        elif self.settings.AUTH_MODE == "basic":
            creds = base64.b64encode(
                f"{self.settings.MAXIMO_USERNAME}:{self.settings.MAXIMO_PASSWORD}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {creds}"
            # Maximo-specific header required by many deployments (7.5/7.6.x)
            headers["maxauth"] = creds

        self._client = httpx.AsyncClient(
            base_url=self.settings.MAXIMO_URL,
            headers=headers,
            timeout=timeout,
            verify=True,
            follow_redirects=True,
            limits=limits,
        )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Authentication ─────────────────────────────────────────────────────

    async def authenticate(self) -> bool:
        """
        Establish authenticated session.
        - apikey: header is already set; do a lightweight ping.
        - basic:  Maximo uses cookie-based sessions; POST to j_security_check.
        - oauth:  Fetch bearer token from token URL.
        """
        if self._client is None:
            await self._init_client()

        if self.settings.AUTH_MODE == "basic":
            return await self._basic_auth_login()
        elif self.settings.AUTH_MODE == "oauth":
            return await self._oauth_login()
        else:
            # apikey — just verify connectivity
            try:
                resp = await self._client.get("/whoami", params={"lean": "1"})
                return resp.status_code == 200
            except Exception as exc:
                logger.warning("apikey ping failed: %s", exc)
                return False

    async def _basic_auth_login(self) -> bool:
        """
        Maximo Basic/Form authentication.
        Tries the REST /login endpoint first, then j_security_check.

        Session cookies must be stored on the same AsyncClient used for OSLC
        requests; a separate client for j_security_check would leave /os calls
        unauthenticated (401) even when the form login succeeded.
        """
        assert self._client is not None

        # Method 1: Maximo REST login endpoint (Maximo 7.6.1+)
        try:
            resp = await self._client.get(
                "/login",
                params={"lean": "1"},
                headers={"maxauth": base64.b64encode(
                    f"{self.settings.MAXIMO_USERNAME}:{self.settings.MAXIMO_PASSWORD}".encode()
                ).decode()},
            )
            if resp.status_code in (200, 204):
                logger.info("Basic auth via /login succeeded")
                return True
        except Exception as exc:
            logger.debug("REST /login failed: %s", exc)

        # Method 2: Form-based j_security_check (same client so JSESSIONID applies to /oslc)
        # Strip auth headers — j_security_check authenticates via POST body only.
        try:
            form_url = f"{self.settings.MAXIMO_HOST}/maximo/j_security_check"
            resp = await self._client.post(
                form_url,
                data={
                    "j_username": self.settings.MAXIMO_USERNAME,
                    "j_password": self.settings.MAXIMO_PASSWORD,
                },
                headers={"Authorization": "", "maxauth": ""},
            )
            if resp.status_code in (200, 302, 303):
                logger.info("Basic auth via j_security_check succeeded")
                return True
        except Exception as exc:
            logger.debug("j_security_check failed: %s", exc)

        return False

    async def _reset_connection(self) -> None:
        """Close and recreate http client (stale keepalive/session)."""
        try:
            await self.close()
        finally:
            await self._init_client()

    async def _reauth_if_needed(self) -> None:
        """Re-establish session for basic auth if possible."""
        if self.settings.AUTH_MODE == "basic":
            try:
                await self._basic_auth_login()
            except Exception as exc:
                logger.warning("Re-auth attempt failed: %s", exc)

    def _debug_log(self, msg: str, **fields: Any) -> None:
        if not getattr(self.settings, "DEBUG_HTTP", False):
            return
        safe = _sanitize({k: _truncate(v) for k, v in fields.items()})
        logger.info("%s %s", msg, safe)

    def _backoff_seconds(self, retry_num: int) -> float:
        base = float(getattr(self.settings, "HTTP_RETRY_BACKOFF_BASE_SECONDS", 0.8) or 0.8)
        cap = float(getattr(self.settings, "HTTP_RETRY_BACKOFF_MAX_SECONDS", 15.0) or 15.0)
        wait = min(cap, base * (2 ** retry_num))
        return max(0.1, wait + random.random() * 0.25)

    async def _oauth_login(self) -> bool:
        """Fetch OAuth 2.0 client_credentials token."""
        if time.time() < self._token_expires_at - 30:
            return True  # Token still valid

        try:
            async with httpx.AsyncClient() as temp:
                resp = await temp.post(
                    self.settings.OAUTH_TOKEN_URL or "",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.OAUTH_CLIENT_ID,
                        "client_secret": self.settings.OAUTH_CLIENT_SECRET,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
                self._oauth_token = payload["access_token"]
                self._token_expires_at = time.time() + payload.get("expires_in", 3600)
                assert self._client is not None
                self._client.headers["Authorization"] = f"Bearer {self._oauth_token}"
                logger.info("OAuth token acquired; expires in %ds", payload.get("expires_in", 3600))
                return True
        except Exception as exc:
            raise MaximoAuthError(f"OAuth login failed: {exc}") from exc

    # ── OSLC Query Builder ─────────────────────────────────────────────────

    def build_oslc_query(
        self,
        where: Optional[str] = None,
        select: Optional[str] = None,
        order_by: Optional[str] = None,
        page_size: Optional[int] = None,
        page_num: int = 1,
        collectioncount: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Build OSLC query parameters dictionary.

        Args:
            where:     OSLC where clause, e.g. 'siteid="BEDFORD" and status="APPR"'
            select:    Comma-separated fields, e.g. 'assetnum,description,status'
            order_by:  Field with optional +/-, e.g. '-changedate'
            page_size: Records per page (max 200). If None, uses DEFAULT_PAGE_SIZE
                       when VPN_SAFE_MODE is enabled, else 50.
            page_num:  1-based page number
            collectioncount: If 1, Maximo includes totalCount in responseInfo (extra SQL cost).

        Returns:
            Dict of OSLC query parameters ready for use in get()
        """
        if page_size is None:
            if getattr(self.settings, "VPN_SAFE_MODE", False):
                page_size = int(getattr(self.settings, "DEFAULT_PAGE_SIZE", 20) or 20)
            else:
                page_size = 50
        # Hard cap: 200 to avoid connection resets on large payloads over VPN
        params: Dict[str, Any] = {
            "lean": "1",
            "oslc.pageSize": min(page_size, 200),
        }
        if where:
            params["oslc.where"] = where
        if select:
            params["oslc.select"] = select
        if order_by:
            params["oslc.orderBy"] = order_by
        if page_num > 1:
            params["pageno"] = page_num
        if collectioncount is not None:
            params["collectioncount"] = collectioncount
        return params

    # ── HTTP Methods ───────────────────────────────────────────────────────

    async def get(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """GET request with automatic lean=1 and retry."""
        p = {"lean": "1", **(params or {})}
        return await self._request("GET", endpoint, params=p)

    async def post(
        self, endpoint: str, body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """POST request."""
        p = {"lean": "1", **(params or {})}
        return await self._request("POST", endpoint, json=body, params=p)

    async def patch(
        self, endpoint: str, body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """PATCH request."""
        p = {"lean": "1", **(params or {})}
        return await self._request("PATCH", endpoint, json=body, params=p)

    async def delete(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """DELETE request."""
        p = {"lean": "1", **(params or {})}
        return await self._request("DELETE", endpoint, params=p)

    # ── Core Request Engine ────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json: Optional[Dict] = None,
        _retry: int = 0,
        _reduce_page: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute an HTTP request with exponential backoff retry on 429/503.
        Parses both OSLC envelope (rdfs:member) and lean (member) responses.
        """
        if self._client is None:
            await self._init_client()

        # Refresh OAuth token if needed
        if self.settings.AUTH_MODE == "oauth":
            await self._oauth_login()

        assert self._client is not None

        # On retry after a transport error, halve page_size to reduce payload
        if _reduce_page and params and "oslc.pageSize" in params:
            current = int(params["oslc.pageSize"])
            reduced = max(5, current // 2)
            if reduced != current:
                logger.warning("Reducing oslc.pageSize %d→%d on retry %d", current, reduced, _retry)
                params = {**params, "oslc.pageSize": reduced}

        start = time.monotonic()
        try:
            self._debug_log("maximo_request", method=method, endpoint=endpoint, params=params or {}, json=json or {})
            resp = await self._client.request(
                method, endpoint, params=params, json=json
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            if _retry < self.settings.HTTP_MAX_RETRIES:
                await self._reset_connection()
                await self._reauth_if_needed()
                await asyncio.sleep(self._backoff_seconds(_retry))
                return await self._request(method, endpoint, params=params, json=json,
                                            _retry=_retry + 1, _reduce_page=True)
            raise MaximoAPIError(f"Request timed out: {exc}") from exc
        except (httpx.NetworkError, httpx.ProtocolError) as exc:
            # Connection reset / write errors / protocol hiccups (covers ReadError,
            # ConnectError, WriteError, RemoteProtocolError, LocalProtocolError).
            if _retry < self.settings.HTTP_MAX_RETRIES:
                logger.warning("Transport error on %s %s; resetting connection and retrying.", method, endpoint)
                await self._reset_connection()
                await self._reauth_if_needed()
                await asyncio.sleep(self._backoff_seconds(_retry))
                return await self._request(method, endpoint, params=params, json=json,
                                            _retry=_retry + 1, _reduce_page=True)
            raise MaximoAPIError(f"Connection error: {exc}") from exc

        duration_ms = int((time.monotonic() - start) * 1000)
        self._debug_log("maximo_response", method=method, endpoint=endpoint, status_code=resp.status_code, duration_ms=duration_ms)
        logger.debug("%s %s → %d (%dms)", method, endpoint, resp.status_code, duration_ms)

        # ── Retry logic ──
        if resp.status_code in (429, 503) and _retry < self.settings.HTTP_MAX_RETRIES:
            wait = 2 ** _retry
            logger.warning("Rate limited (%d). Retrying in %ds...", resp.status_code, wait)
            await asyncio.sleep(wait)
            return await self._request(method, endpoint, params=params, json=json,
                                        _retry=_retry + 1, _reduce_page=True)

        # ── Error handling ──
        if resp.status_code == 401:
            if _retry < self.settings.HTTP_MAX_RETRIES and self.settings.AUTH_MODE == "basic":
                await self._reset_connection()
                await self._reauth_if_needed()
                await asyncio.sleep(self._backoff_seconds(_retry))
                return await self._request(method, endpoint, params=params, json=json, _retry=_retry + 1)
            raise MaximoAuthError("Authentication failed (401). Check credentials.")
        if resp.status_code == 403:
            raise MaximoAPIError("Forbidden (403). Check user permissions.", 403, resp.text)
        if resp.status_code == 404:
            raise MaximoAPIError(f"Resource not found (404): {endpoint}", 404, resp.text)
        if resp.status_code >= 400:
            raise MaximoAPIError(
                f"API error {resp.status_code}: {resp.text[:500]}",
                resp.status_code,
                resp.text,
            )

        # ── Parse response ──
        if resp.status_code == 204 or not resp.content:
            return {"_duration_ms": duration_ms}

        try:
            data = resp.json()
        except Exception:
            txt = (resp.text or "").strip()
            if not txt:
                return {"_duration_ms": duration_ms}
            return {"_raw": txt, "_duration_ms": duration_ms}

        return mask_response(self._parse_response(data, duration_ms))

    @staticmethod
    def _parse_response(data: Any, duration_ms: int) -> Dict[str, Any]:
        """
        Normalize OSLC envelope and lean response formats into a uniform dict.

        OSLC envelope has 'rdfs:member'; lean response has 'member'.
        Single resource responses are plain dicts.
        """
        if isinstance(data, dict):
            # Collection (OSLC or lean)
            members = data.get("member") or data.get("rdfs:member")
            if members is not None:
                # Total is often under responseInfo; root totalCount may be absent unless
                # collectioncount=1 was sent (see IBM Maximo REST selecting guide).
                ri = data.get("responseInfo") or data.get("oslc:responseInfo") or {}
                total: Optional[int] = None
                if isinstance(ri, dict):
                    tc = ri.get("totalCount")
                    if tc is not None:
                        try:
                            total = int(tc)
                        except (TypeError, ValueError):
                            total = None
                if total is None:
                    for key in ("totalCount", "oslc:totalCount"):
                        if key in data and data[key] is not None:
                            try:
                                total = int(data[key])
                                break
                            except (TypeError, ValueError):
                                continue
                if total is None:
                    total = len(members)
                return {
                    "member": members,
                    "totalCount": total,
                    "nextPage": data.get("nextPage") or data.get("oslc:nextPage"),
                    "_duration_ms": duration_ms,
                }
            # Single resource — add duration and return as-is
            data["_duration_ms"] = duration_ms
            return data
        # Array at root (uncommon but possible)
        if isinstance(data, list):
            return {"member": data, "totalCount": len(data), "_duration_ms": duration_ms}
        return {"_raw": data, "_duration_ms": duration_ms}


# ── Module-level singleton helpers ─────────────────────────────────────────────

_client_instance: Optional[MaximoClient] = None


def get_client() -> MaximoClient:
    """Return the shared MaximoClient instance (not yet connected)."""
    global _client_instance
    if _client_instance is None:
        _client_instance = MaximoClient()
    return _client_instance


async def get_connected_client() -> MaximoClient:
    """Return an initialised client; for basic auth, establish Maximo session (cookies)."""
    client = get_client()
    if client._client is None:
        await client._init_client()
        ok = await client.authenticate()
        if not ok and client.settings.AUTH_MODE == "basic":
            logger.warning(
                "Maximo session login did not complete; OSLC calls may return 401."
            )
    return client
